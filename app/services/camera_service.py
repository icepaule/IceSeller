"""Camera service for USB webcam and gphoto2 capture."""

import asyncio
import logging
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Optional

try:
    import cv2
except ImportError:
    cv2 = None

from app.config import settings

logger = logging.getLogger(__name__)


class CameraService:
    """Thread-safe camera service supporting USB webcams and gphoto2."""

    def __init__(self):
        self._lock = threading.Lock()
        self._cap = None

    def _open(self):
        """Open the camera device. Must be called while holding _lock."""
        if cv2 is None:
            raise RuntimeError("OpenCV (cv2) is not installed")
        if self._cap is not None and self._cap.isOpened():
            return self._cap
        device = settings.camera_device
        # Try numeric device index first (e.g. "0" or "/dev/video0")
        try:
            idx = int(device)
        except ValueError:
            idx = device
        self._cap = cv2.VideoCapture(idx)
        if not self._cap.isOpened():
            logger.error("Failed to open camera device: %s", device)
            raise RuntimeError(f"Cannot open camera device {device}")
        # Auto-detect best codec+resolution: try MJPEG 1080p first, fallback YUYV 720p
        self._cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc('M', 'J', 'P', 'G'))
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
        actual_w = self._cap.get(cv2.CAP_PROP_FRAME_WIDTH)
        actual_h = self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
        if actual_h < 1080:
            # Camera doesn't support 1080p MJPEG, try YUYV 720p
            self._cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc('Y', 'U', 'Y', 'V'))
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
            actual_w = self._cap.get(cv2.CAP_PROP_FRAME_WIDTH)
            actual_h = self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
        self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # minimize buffer lag
        # Try to configure focus (LifeCam: manual focus; AKASO: fixed, ignored)
        try:
            subprocess.run(
                ["v4l2-ctl", "-d", settings.camera_device, "--set-ctrl",
                 "focus_automatic_continuous=0"],
                capture_output=True, timeout=5,
            )
            subprocess.run(
                ["v4l2-ctl", "-d", settings.camera_device, "--set-ctrl",
                 "focus_absolute=25,sharpness=50"],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass
        # Let camera auto-expose for a few frames
        for _ in range(15):
            self._cap.read()
        logger.info("Camera opened: %s @ %.0fx%.0f", device, actual_w, actual_h)
        return self._cap

    def _release(self) -> None:
        """Release the camera. Must be called while holding _lock."""
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def _read_frame(self, preview: bool = False) -> bytes:
        """Read a single JPEG-encoded frame from the USB camera.

        Parameters
        ----------
        preview : bool
            If True, scale down to 50% and use lower JPEG quality (for
            the live stream).  If False, return full resolution at high
            quality (for photo capture / Ollama identification).

        Returns the JPEG bytes. Raises RuntimeError on failure.
        """
        with self._lock:
            cap = self._open()
            # Grab (discard) buffered frame to get a fresh one
            cap.grab()
            ret, frame = cap.read()
        if not ret or frame is None:
            raise RuntimeError("Failed to read frame from camera")
        if preview:
            # Scale down for stream (reduce bandwidth, ~5fps anyway)
            h, w = frame.shape[:2]
            if w > 960:
                frame = cv2.resize(frame, (w // 2, h // 2), interpolation=cv2.INTER_AREA)
            quality = 70
        else:
            # Full resolution capture -- enhance for article images
            frame = self._enhance_capture(frame)
            quality = 95
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            raise RuntimeError("Failed to encode frame as JPEG")
        return buf.tobytes()

    @staticmethod
    def _enhance_capture(frame):
        """Enhance a captured frame: light denoise, gentle sharpen, contrast."""
        import numpy as np
        # 1. Light denoise while preserving edges
        frame = cv2.bilateralFilter(frame, d=5, sigmaColor=40, sigmaSpace=40)
        # 2. Gentle unsharp mask (LifeCam has good optics, less correction needed)
        blurred = cv2.GaussianBlur(frame, (0, 0), 1.5)
        frame = cv2.addWeighted(frame, 1.5, blurred, -0.5, 0)
        # 3. CLAHE on L channel -- boost local contrast (makes text readable)
        lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        l = clahe.apply(l)
        frame = cv2.cvtColor(cv2.merge([l, a, b]), cv2.COLOR_LAB2BGR)
        return frame

    # ------------------------------------------------------------------
    # MJPEG stream
    # ------------------------------------------------------------------

    async def mjpeg_stream(self):
        """Async generator yielding MJPEG multipart frames.

        Each yielded chunk is a complete multipart segment including
        Content-Type and Content-Length headers so that it can be sent
        directly inside a ``multipart/x-mixed-replace`` response.
        """
        loop = asyncio.get_event_loop()
        fail_count = 0
        try:
            while True:
                try:
                    jpeg = await loop.run_in_executor(
                        None, lambda: self._read_frame(preview=True)
                    )
                    fail_count = 0
                except RuntimeError as e:
                    fail_count += 1
                    logger.warning("Camera read failure #%d: %s", fail_count, e)
                    if fail_count > 20:
                        logger.error("Too many camera failures, stopping stream")
                        return
                    await asyncio.sleep(1.0)
                    continue
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n"
                    b"\r\n" + jpeg + b"\r\n"
                )
                # ~5 fps (stable, low flicker for preview)
                await asyncio.sleep(0.2)
        finally:
            with self._lock:
                self._release()

    # ------------------------------------------------------------------
    # Photo capture
    # ------------------------------------------------------------------

    def _generate_filename(self) -> str:
        """Return a unique filename for a captured image."""
        return f"{uuid.uuid4().hex}.jpg"

    def capture_usb(self) -> str:
        """Capture a single frame from the USB camera and save to disk.

        Returns the filename (relative to images_dir).
        """
        jpeg = self._read_frame()
        filename = self._generate_filename()
        filepath = settings.images_dir / filename
        filepath.write_bytes(jpeg)
        logger.info("USB capture saved: %s", filepath)
        return filename

    def capture_gphoto2(self) -> str:
        """Capture an image using gphoto2 (DSLR / mirrorless).

        Calls ``gphoto2 --capture-image-and-download`` and moves the
        resulting file into images_dir with a UUID filename.

        Returns the filename (relative to images_dir).
        """
        filename = self._generate_filename()
        dest = settings.images_dir / filename
        try:
            result = subprocess.run(
                [
                    "gphoto2",
                    "--capture-image-and-download",
                    "--filename",
                    str(dest),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                logger.error("gphoto2 error: %s", result.stderr)
                raise RuntimeError(f"gphoto2 failed: {result.stderr.strip()}")
        except FileNotFoundError:
            raise RuntimeError("gphoto2 is not installed")
        logger.info("gphoto2 capture saved: %s", dest)
        return filename

    def capture(self) -> str:
        """Capture a photo using the configured camera_type.

        Returns the filename (relative to images_dir).
        """
        if settings.camera_type == "gphoto2":
            return self.capture_gphoto2()
        return self.capture_usb()

    # ------------------------------------------------------------------
    # PTZ control via v4l2-ctl
    # ------------------------------------------------------------------

    # (control_name, step_delta, min, max)
    # LifeCam Cinema: pan/tilt -201600..201600 step 3600, zoom 0..10, focus 0..40
    _PTZ_DEFS = {
        "pan_left":    ("pan_absolute",  -3600, -201600, 201600),
        "pan_right":   ("pan_absolute",   3600, -201600, 201600),
        "tilt_up":     ("tilt_absolute",  3600, -201600, 201600),
        "tilt_down":   ("tilt_absolute", -3600, -201600, 201600),
        "zoom_in":     ("zoom_absolute",     1,       0,     10),
        "zoom_out":    ("zoom_absolute",    -1,       0,     10),
        "focus_near":  ("focus_absolute",    5,       0,     40),
        "focus_far":   ("focus_absolute",   -5,       0,     40),
    }

    def _v4l2_get(self, ctrl: str) -> int:
        """Read current value of a v4l2 control."""
        try:
            result = subprocess.run(
                ["v4l2-ctl", "-d", settings.camera_device, "--get-ctrl", ctrl],
                capture_output=True, text=True, timeout=5,
            )
            # output: "pan_absolute: 3600"
            return int(result.stdout.strip().split(":")[-1].strip())
        except Exception:
            return 0

    def ptz(self, direction: str) -> dict:
        """Send a relative PTZ step via v4l2-ctl.

        Reads the current value, adds the step delta (clamped to min/max),
        and sets the new value.

        Returns dict with the new control value.
        """
        if direction not in self._PTZ_DEFS:
            raise ValueError(
                f"Unknown PTZ direction: {direction!r}. "
                f"Valid: {', '.join(self._PTZ_DEFS)}"
            )
        ctrl, delta, lo, hi = self._PTZ_DEFS[direction]
        current = self._v4l2_get(ctrl)
        new_val = max(lo, min(hi, current + delta))
        cmd = [
            "v4l2-ctl", "-d", settings.camera_device,
            "--set-ctrl", f"{ctrl}={new_val}",
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                logger.warning("v4l2-ctl error: %s", result.stderr)
                raise RuntimeError(f"v4l2-ctl failed: {result.stderr.strip()}")
        except FileNotFoundError:
            raise RuntimeError("v4l2-ctl is not installed")
        logger.debug("PTZ %s: %s %d -> %d", direction, ctrl, current, new_val)
        return {"control": ctrl, "value": new_val}

    # ------------------------------------------------------------------
    # Crop capture
    # ------------------------------------------------------------------

    def capture_usb_cropped(self, crop: dict) -> str:
        """Capture full-res frame, crop to selection, save.

        Parameters
        ----------
        crop : dict
            {x, y, w, h} as fractions 0.0-1.0 of the frame dimensions.

        Returns the filename (relative to images_dir).
        """
        if cv2 is None:
            raise RuntimeError("OpenCV (cv2) is not installed")
        with self._lock:
            cap = self._open()
            ret, frame = cap.read()
        if not ret or frame is None:
            raise RuntimeError("Failed to read frame from camera")

        fh, fw = frame.shape[:2]
        x = int(crop["x"] * fw)
        y = int(crop["y"] * fh)
        w = int(crop["w"] * fw)
        h = int(crop["h"] * fh)
        # Clamp
        x = max(0, min(x, fw - 1))
        y = max(0, min(y, fh - 1))
        w = max(1, min(w, fw - x))
        h = max(1, min(h, fh - y))
        frame = frame[y:y+h, x:x+w]

        # Enforce minimum resolution for OCR readability (labels need pixels!)
        crop_h, crop_w = frame.shape[:2]
        min_dim = 800
        if max(crop_w, crop_h) < min_dim:
            scale = min_dim / max(crop_w, crop_h)
            new_w = int(crop_w * scale)
            new_h = int(crop_h * scale)
            frame = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4)
            logger.info("Upscaled crop from %dx%d to %dx%d (min %dpx)", crop_w, crop_h, new_w, new_h, min_dim)

        # Enhance cropped capture
        frame = self._enhance_capture(frame)

        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
        if not ok:
            raise RuntimeError("Failed to encode cropped frame")
        filename = self._generate_filename()
        (settings.images_dir / filename).write_bytes(buf.tobytes())
        final_h, final_w = frame.shape[:2]
        logger.info("Cropped capture saved: %s (%dx%d from %dx%d)", filename, final_w, final_h, fw, fh)
        return filename


# Module-level singleton
camera_service = CameraService()
