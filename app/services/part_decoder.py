"""Deterministic RAM part number decoder.

Decodes manufacturer part numbers for RAM modules using fixed schemas.
No LLM, no guessing — pure logic based on manufacturer naming conventions.
"""

import logging
import re

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SK hynix: HMT (DDR3), HMA (DDR4), HMCG/HMAG (DDR5)
# Example: HMA81GS6CJR8N-XN
# ---------------------------------------------------------------------------

_HYNIX_PREFIX_GEN = {
    "HMT": "DDR3",
    "HMA": "DDR4",
    "HMCG": "DDR5",
    "HMAG": "DDR5",
}

_HYNIX_DENSITY_GBIT = {
    "2": 2,
    "4": 4,
    "8": 8,
    "16": 16,
}

_HYNIX_FORM = {
    "G": "SODIMM",
    "U": "UDIMM",
    "R": "RDIMM",
    "E": "ECC UDIMM",
    "A": "UDIMM",  # alternate DDR5 code
}

_HYNIX_SPEED_SUFFIX = {
    # DDR4
    "XN": ("3200", "PC4-25600"),
    "JJ": ("2666", "PC4-21300"),
    "VK": ("2666", "PC4-21300"),
    "DY": ("2400", "PC4-19200"),
    "AF": ("2400", "PC4-19200"),
    "TF": ("2133", "PC4-17000"),
    # DDR3
    "PB": ("1600", "PC3-12800"),
    "SK": ("1600", "PC3-12800"),
    "CK": ("1600", "PC3-12800"),
    "MR": ("1333", "PC3-10600"),
    "MP": ("1066", "PC3-8500"),
}

_HYNIX_VOLTAGE = {
    "DDR3": "1.5V",
    "DDR4": "1.2V",
    "DDR5": "1.1V",
}

_HYNIX_PINS = {
    "SODIMM": {"DDR3": "204-Pin", "DDR4": "260-Pin", "DDR5": "262-Pin"},
    "UDIMM": {"DDR3": "240-Pin", "DDR4": "288-Pin", "DDR5": "288-Pin"},
    "RDIMM": {"DDR3": "240-Pin", "DDR4": "288-Pin", "DDR5": "288-Pin"},
    "ECC UDIMM": {"DDR3": "240-Pin", "DDR4": "288-Pin", "DDR5": "288-Pin"},
}


def _decode_hynix(mpn: str) -> dict | None:
    """Decode SK hynix part number."""
    upper = mpn.upper()

    # Determine DDR generation from prefix
    ddr_gen = None
    prefix_len = 0
    for prefix, gen in _HYNIX_PREFIX_GEN.items():
        if upper.startswith(prefix):
            ddr_gen = gen
            prefix_len = len(prefix)
            break
    if not ddr_gen:
        return None

    rest = upper[prefix_len:]  # e.g. "81GS6CJR8N-XN"
    if len(rest) < 3:
        return None

    # Density digit (position 0 after prefix)
    density_gbit = _HYNIX_DENSITY_GBIT.get(rest[0])
    if density_gbit is None:
        return None

    # Ranks digit (position 1 after prefix)
    rank_digit = rest[1]
    if rank_digit in ("1", "2", "4"):
        ranks = f"{rank_digit}Rx8" if rank_digit == "1" else f"{rank_digit}Rx4" if rank_digit == "4" else f"{rank_digit}Rx8"
    else:
        ranks = None

    # Form factor letter (position 2 after prefix)
    form_letter = rest[2]
    form_factor = _HYNIX_FORM.get(form_letter)
    if not form_factor:
        return None

    # Calculate capacity: for x8 org on 64-bit bus, capacity = density_gbit * ranks
    # (8 chips/rank × density_gbit / 8 bits/byte = density_gbit GB per rank)
    rank_count = int(rank_digit) if rank_digit.isdigit() else 1
    capacity_gb = density_gbit * rank_count

    # Speed suffix after "-"
    speed_mhz = None
    pc_rating = None
    if "-" in mpn:
        suffix = upper.split("-", 1)[1]
        for code, (mhz, pc) in _HYNIX_SPEED_SUFFIX.items():
            if suffix.startswith(code):
                speed_mhz = mhz
                pc_rating = pc
                break

    voltage = _HYNIX_VOLTAGE.get(ddr_gen, "")
    pins = _HYNIX_PINS.get(form_factor, {}).get(ddr_gen, "")

    result = {
        "manufacturer": "SK hynix",
        "type": f"{ddr_gen} {form_factor}",
        "capacity": f"{capacity_gb}GB",
        "form_factor": f"{form_factor} ({pins})" if pins else form_factor,
        "voltage": voltage,
    }
    if ranks:
        result["ranks"] = ranks
    if speed_mhz:
        result["speed"] = f"{ddr_gen}-{speed_mhz} ({pc_rating})"
    return result


# ---------------------------------------------------------------------------
# Samsung: M471 (DDR4 SODIMM), M378 (DDR4 UDIMM), M393 (DDR4 RDIMM), etc.
# Example: M471A1K43DB1-CTD
# ---------------------------------------------------------------------------

_SAMSUNG_MODULE_TYPE = {
    "M471": ("DDR4", "SODIMM"),
    "M378": ("DDR4", "UDIMM"),
    "M393": ("DDR4", "RDIMM"),
    "M474": ("DDR4", "SODIMM ECC"),
    "M391": ("DDR4", "ECC UDIMM"),
    "M473": ("DDR3", "SODIMM"),
    "M471B": ("DDR3", "SODIMM"),
    "M378B": ("DDR3", "UDIMM"),
    "M393B": ("DDR3", "RDIMM"),
}

_SAMSUNG_SPEED_SUFFIX = {
    "CWE": ("3200", "PC4-25600"),
    "CVF": ("3200", "PC4-25600"),
    "CTD": ("2666", "PC4-21300"),
    "CRC": ("2400", "PC4-19200"),
    "CRF": ("2400", "PC4-19200"),
    "CMA": ("2133", "PC4-17000"),
    "CPB": ("1600", "PC3-12800"),
    "CFN": ("1333", "PC3-10600"),
}

# Samsung density+org codes to capacity (GB)
_SAMSUNG_CAPACITY = {
    "A1K43": 8,
    "A1K44": 8,
    "A1K4": 8,
    "A1G43": 8,
    "A1G44": 8,
    "A2K43": 16,
    "A2K42": 16,
    "A2G43": 16,
    "A2G44": 16,
    "A4G43": 32,
    "A5K44": 4,
    "A5K43": 4,
    "A5G43": 4,
    "B1G73": 8,
    "B5273": 4,
    "B5173": 4,
    "B1G7": 8,
    "B5G7": 4,
}


def _decode_samsung(mpn: str) -> dict | None:
    """Decode Samsung part number."""
    upper = mpn.upper()

    ddr_gen = None
    form_factor = None

    # Try longer prefixes first (M471B before M471)
    for prefix in sorted(_SAMSUNG_MODULE_TYPE.keys(), key=len, reverse=True):
        if upper.startswith(prefix):
            ddr_gen, form_factor = _SAMSUNG_MODULE_TYPE[prefix]
            rest = upper[len(prefix):]
            break
    else:
        return None

    # Try to extract capacity from density+org code
    capacity_gb = None
    for code, cap in sorted(_SAMSUNG_CAPACITY.items(), key=lambda x: len(x[0]), reverse=True):
        if rest.startswith(code):
            capacity_gb = cap
            break

    # Speed suffix after "-"
    speed_mhz = None
    pc_rating = None
    if "-" in mpn:
        suffix = upper.split("-", 1)[1]
        for code, (mhz, pc) in _SAMSUNG_SPEED_SUFFIX.items():
            if suffix.startswith(code):
                speed_mhz = mhz
                pc_rating = pc
                break

    voltage = "1.2V" if "DDR4" in ddr_gen else "1.5V" if "DDR3" in ddr_gen else ""
    base_form = form_factor.split(" ")[0]  # "SODIMM" from "SODIMM ECC"
    pins_map = {
        "SODIMM": {"DDR3": "204-Pin", "DDR4": "260-Pin"},
        "UDIMM": {"DDR3": "240-Pin", "DDR4": "288-Pin"},
        "RDIMM": {"DDR3": "240-Pin", "DDR4": "288-Pin"},
        "ECC": {"DDR3": "240-Pin", "DDR4": "288-Pin"},
    }
    pins = pins_map.get(base_form, {}).get(ddr_gen, "")

    result = {
        "manufacturer": "Samsung",
        "type": f"{ddr_gen} {form_factor}",
        "form_factor": f"{form_factor} ({pins})" if pins else form_factor,
        "voltage": voltage,
    }
    if capacity_gb:
        result["capacity"] = f"{capacity_gb}GB"
    if speed_mhz:
        result["speed"] = f"{ddr_gen}-{speed_mhz} ({pc_rating})"
    return result


# ---------------------------------------------------------------------------
# Kingston: KVR (ValueRAM), KF (FURY)
# Example: KVR32S22S8/16
# ---------------------------------------------------------------------------

_KINGSTON_FORM = {
    "S": "SODIMM",
    "N": "UDIMM",
    "R": "RDIMM",
    "E": "ECC UDIMM",
}


def _decode_kingston(mpn: str) -> dict | None:
    """Decode Kingston part number."""
    upper = mpn.upper()

    # KVR or KF prefix
    if not (upper.startswith("KVR") or upper.startswith("KF")):
        return None

    # Extract speed digits: KVR32... or KF32...
    prefix_len = 3 if upper.startswith("KVR") else 2
    rest = upper[prefix_len:]

    # Speed is first 2 digits
    speed_match = re.match(r"(\d{2})", rest)
    if not speed_match:
        return None
    speed_code = speed_match.group(1)

    speed_map = {
        "48": ("4800", "DDR5", "PC5-38400"),
        "56": ("5600", "DDR5", "PC5-44800"),
        "32": ("3200", "DDR4", "PC4-25600"),
        "26": ("2666", "DDR4", "PC4-21300"),
        "24": ("2400", "DDR4", "PC4-19200"),
        "21": ("2133", "DDR4", "PC4-17000"),
        "16": ("1600", "DDR3", "PC3-12800"),
        "13": ("1333", "DDR3", "PC3-10600"),
    }
    if speed_code not in speed_map:
        return None

    speed_mhz, ddr_gen, pc_rating = speed_map[speed_code]

    # Form factor letter follows speed
    rest_after_speed = rest[2:]
    form_factor = None
    for letter, ff in _KINGSTON_FORM.items():
        if letter in rest_after_speed[:3]:
            form_factor = ff
            break
    if not form_factor:
        form_factor = "UDIMM"  # default

    # Capacity after "/"
    capacity_gb = None
    if "/" in mpn:
        cap_str = mpn.split("/")[-1]
        cap_match = re.match(r"(\d+)", cap_str)
        if cap_match:
            capacity_gb = int(cap_match.group(1))

    voltage_map = {"DDR3": "1.5V", "DDR4": "1.2V", "DDR5": "1.1V"}
    voltage = voltage_map.get(ddr_gen, "")

    pins_map = {
        "SODIMM": {"DDR3": "204-Pin", "DDR4": "260-Pin", "DDR5": "262-Pin"},
        "UDIMM": {"DDR3": "240-Pin", "DDR4": "288-Pin", "DDR5": "288-Pin"},
        "RDIMM": {"DDR3": "240-Pin", "DDR4": "288-Pin", "DDR5": "288-Pin"},
    }
    base_form = form_factor.split(" ")[0]
    pins = pins_map.get(base_form, {}).get(ddr_gen, "")

    brand = "Kingston FURY" if upper.startswith("KF") else "Kingston"

    result = {
        "manufacturer": brand,
        "type": f"{ddr_gen} {form_factor}",
        "form_factor": f"{form_factor} ({pins})" if pins else form_factor,
        "voltage": voltage,
    }
    if capacity_gb:
        result["capacity"] = f"{capacity_gb}GB"
    result["speed"] = f"{ddr_gen}-{speed_mhz} ({pc_rating})"
    return result


# ---------------------------------------------------------------------------
# Micron: MTA (DDR4), MTC (DDR5)
# Example: MTA8ATF1G64HZ-3G2R1
# ---------------------------------------------------------------------------

_MICRON_FORM = {
    "HZ": "SODIMM",
    "AZ": "UDIMM",
    "PZ": "RDIMM",
    "PF": "RDIMM",
    "HR": "SODIMM",
}

_MICRON_SPEED_SUFFIX = {
    "3G2": ("3200", "DDR4", "PC4-25600"),
    "2G6": ("2666", "DDR4", "PC4-21300"),
    "2G4": ("2400", "DDR4", "PC4-19200"),
    "2G1": ("2133", "DDR4", "PC4-17000"),
    "1G6": ("1600", "DDR3", "PC3-12800"),
    "1G3": ("1333", "DDR3", "PC3-10600"),
    "4G8": ("4800", "DDR5", "PC5-38400"),
    "5G6": ("5600", "DDR5", "PC5-44800"),
}

# Micron density codes to capacity in GB
_MICRON_CAPACITY = {
    "4ATF51264": 4,
    "8ATF1G64": 8,
    "16ATF2G64": 16,
    "4ATF1G64": 8,
    "8ATF2G64": 16,
    "16ATF4G64": 32,
    "4ATF25664": 2,
}


def _decode_micron(mpn: str) -> dict | None:
    """Decode Micron part number."""
    upper = mpn.upper()

    if upper.startswith("MTA"):
        ddr_default = "DDR4"
        rest = upper[3:]
    elif upper.startswith("MTC"):
        ddr_default = "DDR5"
        rest = upper[3:]
    elif upper.startswith("MT"):
        ddr_default = "DDR4"
        rest = upper[2:]
    else:
        return None

    # Capacity from density code
    capacity_gb = None
    for code, cap in sorted(_MICRON_CAPACITY.items(), key=lambda x: len(x[0]), reverse=True):
        if rest.startswith(code):
            capacity_gb = cap
            break

    # Form factor from 2-letter code before "-"
    form_factor = None
    pre_dash = upper.split("-")[0] if "-" in upper else upper
    for code, ff in _MICRON_FORM.items():
        if pre_dash.endswith(code):
            form_factor = ff
            break

    # Speed suffix after "-"
    ddr_gen = ddr_default
    speed_mhz = None
    pc_rating = None
    if "-" in mpn:
        suffix = upper.split("-", 1)[1]
        for code, (mhz, gen, pc) in _MICRON_SPEED_SUFFIX.items():
            if suffix.startswith(code):
                speed_mhz = mhz
                ddr_gen = gen
                pc_rating = pc
                break

    if not form_factor:
        form_factor = "SODIMM"  # common default

    voltage_map = {"DDR3": "1.5V", "DDR4": "1.2V", "DDR5": "1.1V"}
    voltage = voltage_map.get(ddr_gen, "")

    pins_map = {
        "SODIMM": {"DDR3": "204-Pin", "DDR4": "260-Pin", "DDR5": "262-Pin"},
        "UDIMM": {"DDR3": "240-Pin", "DDR4": "288-Pin", "DDR5": "288-Pin"},
        "RDIMM": {"DDR3": "240-Pin", "DDR4": "288-Pin", "DDR5": "288-Pin"},
    }
    pins = pins_map.get(form_factor, {}).get(ddr_gen, "")

    result = {
        "manufacturer": "Micron",
        "type": f"{ddr_gen} {form_factor}",
        "form_factor": f"{form_factor} ({pins})" if pins else form_factor,
        "voltage": voltage,
    }
    if capacity_gb:
        result["capacity"] = f"{capacity_gb}GB"
    if speed_mhz:
        result["speed"] = f"{ddr_gen}-{speed_mhz} ({pc_rating})"
    return result


# ---------------------------------------------------------------------------
# Crucial: CT prefix
# Example: CT8G4SFS832A
# ---------------------------------------------------------------------------

_CRUCIAL_FORM = {
    "S": "SODIMM",
    "D": "UDIMM",
}


def _decode_crucial(mpn: str) -> dict | None:
    """Decode Crucial part number."""
    upper = mpn.upper()

    if not upper.startswith("CT"):
        return None

    # Capacity: CT8G = 8GB, CT16G = 16GB, CT4G = 4GB, CT32G = 32GB
    cap_match = re.match(r"CT(\d+)G", upper)
    if not cap_match:
        return None
    capacity_gb = int(cap_match.group(1))

    # DDR generation: 4 = DDR4, 5 = DDR5, 3 = DDR3
    gen_match = re.match(r"CT\d+G(\d)", upper)
    if not gen_match:
        return None
    gen_digit = gen_match.group(1)
    gen_map = {"3": "DDR3", "4": "DDR4", "5": "DDR5"}
    ddr_gen = gen_map.get(gen_digit)
    if not ddr_gen:
        return None

    # Form factor: S = SODIMM, D = UDIMM (after the generation+type chars)
    form_factor = "UDIMM"  # default
    rest_after_gen = upper[len(cap_match.group(0)) + 1:]  # skip "CT8G4"
    for letter, ff in _CRUCIAL_FORM.items():
        if letter in rest_after_gen[:3]:
            form_factor = ff
            break

    # Speed code: 832 = 3200, 826 = 2666, 824 = 2400, etc.
    speed_match = re.search(r"(\d{3,4})[A-Z]*$", upper.rstrip(".-"))
    speed_mhz = None
    pc_rating = None
    if speed_match:
        speed_code = speed_match.group(1)
        speed_decode = {
            "832": ("3200", "PC4-25600"),
            "8320": ("3200", "PC4-25600"),
            "426": ("2666", "PC4-21300"),
            "4266": ("2666", "PC4-21300"),
            "424": ("2400", "PC4-19200"),
            "4240": ("2400", "PC4-19200"),
            "213": ("2133", "PC4-17000"),
            "2133": ("2133", "PC4-17000"),
            "160": ("1600", "PC3-12800"),
            "1600": ("1600", "PC3-12800"),
            "480": ("4800", "PC5-38400"),
            "4800": ("4800", "PC5-38400"),
            "560": ("5600", "PC5-44800"),
            "5600": ("5600", "PC5-44800"),
        }
        if speed_code in speed_decode:
            speed_mhz, pc_rating = speed_decode[speed_code]

    voltage_map = {"DDR3": "1.5V", "DDR4": "1.2V", "DDR5": "1.1V"}
    voltage = voltage_map.get(ddr_gen, "")

    pins_map = {
        "SODIMM": {"DDR3": "204-Pin", "DDR4": "260-Pin", "DDR5": "262-Pin"},
        "UDIMM": {"DDR3": "240-Pin", "DDR4": "288-Pin", "DDR5": "288-Pin"},
    }
    pins = pins_map.get(form_factor, {}).get(ddr_gen, "")

    result = {
        "manufacturer": "Crucial",
        "type": f"{ddr_gen} {form_factor}",
        "capacity": f"{capacity_gb}GB",
        "form_factor": f"{form_factor} ({pins})" if pins else form_factor,
        "voltage": voltage,
    }
    if speed_mhz:
        result["speed"] = f"{ddr_gen}-{speed_mhz} ({pc_rating})"
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Ordered list of decoder functions to try
_DECODERS = [
    (re.compile(r"^HM[TAC]", re.IGNORECASE), _decode_hynix),
    (re.compile(r"^M(471|378|393|474|391|473)", re.IGNORECASE), _decode_samsung),
    (re.compile(r"^K(VR|F)\d", re.IGNORECASE), _decode_kingston),
    (re.compile(r"^MT[AC]?\d", re.IGNORECASE), _decode_micron),
    (re.compile(r"^CT\d+G", re.IGNORECASE), _decode_crucial),
]


def decode_ram_part_number(mpn: str) -> dict | None:
    """Decode a RAM part number into specs using deterministic logic.

    Returns a dict with decoded specs on success, or None if the part number
    is not recognized. Supports SK hynix, Samsung, Kingston, Micron, Crucial.

    Example:
        >>> decode_ram_part_number("HMA81GS6CJR8N-XN")
        {'manufacturer': 'SK hynix', 'type': 'DDR4 SODIMM', 'capacity': '8GB',
         'speed': 'DDR4-3200 (PC4-25600)', 'form_factor': 'SODIMM (260-Pin)',
         'voltage': '1.2V', 'ranks': '1Rx8'}
    """
    if not mpn or not isinstance(mpn, str):
        return None

    mpn = mpn.strip()
    if len(mpn) < 4:
        return None

    for pattern, decoder in _DECODERS:
        if pattern.match(mpn):
            try:
                result = decoder(mpn)
                if result:
                    logger.info("Decoded %s → %s", mpn, result)
                    return result
            except Exception:
                logger.warning("Decoder failed for %s", mpn, exc_info=True)

    logger.debug("No decoder matched part number: %s", mpn)
    return None
