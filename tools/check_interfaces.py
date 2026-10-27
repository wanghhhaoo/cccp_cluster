"""ROS 2 .msg/.srv syntax check (rosidl_adapter rules) for machines without ROS.

Usage: python3 tools/check_interfaces.py ros2_ws/src
Checks every *_interfaces package: field and constant names, types, srv separators, CMakeLists lists.
"""
import re
import sys
from pathlib import Path

PRIMITIVES = {"bool", "byte", "char", "float32", "float64", "int8", "uint8", "int16", "uint16",
              "int32", "uint32", "int64", "uint64", "string", "wstring"}
EXTERNAL = {"builtin_interfaces/Time", "builtin_interfaces/Duration", "sensor_msgs/Image"}
FIELD = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")
CONST = re.compile(r"^[A-Z][A-Z0-9]*(_[A-Z0-9]+)*$")


def check_section(lines, pkg, local_msgs, where, errors):
    names = set()
    for no, raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        is_const = re.match(r"^\S+\s+[A-Za-z0-9_]+\s*=", line)
        if not is_const and "#" in line:
            line = line[:line.index("#")].strip()
        m = re.match(r"^(\S+)\s+([A-Za-z0-9_]+)\s*(=.*)?(\s+.*)?$", line)
        if not m:
            errors.append(f"{where}:{no}: cannot parse: {raw!r}")
            continue
        typ, name, const_val = m.group(1), m.group(2), m.group(3)
        base = re.sub(r"\[.*\]$", "", typ)
        if base not in PRIMITIVES and base not in EXTERNAL:
            ref = base.split("/")[-1] if base.startswith(pkg + "/") or "/" not in base else None
            if ref is None or ref not in local_msgs:
                errors.append(f"{where}:{no}: unknown type {typ}")
        if const_val is not None:
            if not CONST.match(name):
                errors.append(f"{where}:{no}: bad constant name {name}")
            if base not in PRIMITIVES or typ != base:
                errors.append(f"{where}:{no}: constant must be a primitive scalar: {typ}")
            value = const_val[1:].strip()
            if base == "string":
                if not re.fullmatch(r'"[^"#]*"', value):
                    errors.append(f"{where}:{no}: string constant must be a quoted value without '#': {value}")
            elif not re.fullmatch(r"-?\d+(\.\d+)?", value):
                errors.append(f"{where}:{no}: bad numeric constant value {value}")
        elif not FIELD.match(name):
            errors.append(f"{where}:{no}: bad field name {name}")
        if name in names:
            errors.append(f"{where}:{no}: duplicate name {name}")
        names.add(name)


def main(root):
    errors = []
    for pkg_dir in sorted(Path(root).glob("*_interfaces")):
        pkg = pkg_dir.name
        files = sorted(pkg_dir.glob("msg/*.msg")) + sorted(pkg_dir.glob("srv/*.srv"))
        local_msgs = {f.stem for f in pkg_dir.glob("msg/*.msg")}
        cmake = (pkg_dir / "CMakeLists.txt").read_text()
        listed = set(re.findall(r'"((?:msg|srv)/[A-Za-z0-9]+\.(?:msg|srv))"', cmake))
        present = {str(f.relative_to(pkg_dir)) for f in files}
        if listed != present:
            errors.append(f"{pkg}: CMakeLists mismatch, missing={present - listed} extra={listed - present}")
        if f"<name>{pkg}</name>" not in (pkg_dir / "package.xml").read_text():
            errors.append(f"{pkg}: package.xml name mismatch")
        for f in files:
            numbered = list(enumerate(f.read_text().splitlines(), 1))
            if f.suffix == ".srv":
                seps = [i for i, (_, l) in enumerate(numbered) if l.strip() == "---"]
                if len(seps) != 1:
                    errors.append(f"{f}: expected exactly one '---', found {len(seps)}")
                    continue
                sections = [numbered[:seps[0]], numbered[seps[0] + 1:]]
            else:
                sections = [numbered]
            for section in sections:
                check_section(section, pkg, local_msgs, f"{pkg}/{f.relative_to(pkg_dir)}", errors)
        print(f"{pkg}: {len(files)} files checked")
    for e in errors:
        print("ERROR", e)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
