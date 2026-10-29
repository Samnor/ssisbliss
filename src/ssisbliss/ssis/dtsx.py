"""Read the parts of a .dtsx package that matter for deployment safety."""
from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

DTS = "www.microsoft.com/SqlServer/Dts"
_NS = {"DTS": DTS}

PROTECTION_LEVELS = {
    0: "DontSaveSensitive",
    1: "EncryptSensitiveWithUserKey",
    2: "EncryptSensitiveWithPassword",
    3: "EncryptAllWithPassword",
    4: "EncryptAllWithUserKey",
    5: "ServerStorage",
}
DONT_SAVE_SENSITIVE, SERVER_STORAGE = 0, 5

# SQL Server major version -> highest PackageFormatVersion it reads.
MAX_PACKAGE_FORMAT = {11: 6, 12: 6, 13: 8, 14: 8, 15: 8, 16: 8, 17: 8}

_PROJECT_PARAM = re.compile(r"\$Project::(\w+)")
_PROJECT_CONMGR = re.compile(r"Project\.ConnectionManagers\[([^\]]+)\]")
_SSIS_ASSEMBLY = re.compile(r"Microsoft\.SqlServer\.[\w.]+,\s*Version=(\d+)\.0\.0\.0")


class PackageError(ValueError):
    pass


@dataclass
class PackageInfo:
    file_name: str
    object_name: str
    dtsid: str
    version_guid: str
    protection_level: int
    package_format_version: int | None
    sha256: str
    project_parameters: set[str] = field(default_factory=set)
    project_connection_managers: set[str] = field(default_factory=set)
    max_assembly_major: int | None = None

    @property
    def protection_level_name(self) -> str:
        return PROTECTION_LEVELS.get(self.protection_level, str(self.protection_level))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# The catalog re-saves every package of a project whenever any of them is deployed (seen on 15.0.4470
# with catalog.deploy_packages): attributes reordered, ProtectionLevel ServerStorage, a new VersionGUID,
# LastModifiedProductVersion set to the server's version, connection properties its runtime does not know
# dropped (Visual Studio 2022 adds ConnectRetryCount, ConnectRetryInterval, ConnectByProxy), and every
# value that a property expression drives recalculated with the deployed parameter values. Package
# identity is the canonical XML without any of that.
_ROOT_VOLATILE = ("ProtectionLevel", "VersionGUID", "LastModifiedProductVersion")
_NEWER_RUNTIME = ("ConnectRetryCount", "ConnectRetryInterval", "ConnectByProxy")
ET.register_namespace("DTS", DTS)


def package_sha256(data: bytes) -> str:
    """Content hash of a package that is the same for a deployed file and the catalog's copy of it."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return sha256(data)
    q = lambda n: f"{{{DTS}}}{n}"  # noqa: E731
    for name in _ROOT_VOLATILE:
        root.attrib.pop(q(name), None)
    for el in root.iter():
        for name in _NEWER_RUNTIME:
            el.attrib.pop(q(name), None)
        derived = {x.get(q("Name")) for x in el.findall(q("PropertyExpression"))}
        for name in derived - {None}:
            # The static copy of an expression-driven value: on the element, on the object data
            # under it (connection managers), or as a DTS:Property child.
            for holder in [el, *[c for d in el.findall(q("ObjectData")) for c in d]]:
                if q(name) in holder.attrib:
                    holder.set(q(name), "")
            for prop in el.findall(q("Property")):
                if prop.get(q("Name")) == name:
                    prop.text = ""
    return sha256(ET.canonicalize(ET.tostring(root, encoding="unicode")).encode())


def as_editable(data: bytes) -> bytes:
    """A package exported from the catalog, with ServerStorage replaced by DontSaveSensitive so it
    fits a Visual Studio project again. The export never contains sensitive values."""
    return re.sub(rb'(DTS:ProtectionLevel=")5(")', rb"\g<1>0\g<2>", data, count=1)


def read_package(file_name: str, data: bytes) -> PackageInfo:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise PackageError(f"{file_name}: not well-formed XML ({e})") from e
    if root.tag != f"{{{DTS}}}Executable" or root.get(f"{{{DTS}}}ExecutableType") != "Microsoft.Package":
        raise PackageError(f"{file_name}: not an SSIS package (root is {root.tag})")

    attr = lambda name: root.get(f"{{{DTS}}}{name}")  # noqa: E731
    # A missing ProtectionLevel attribute means the default, EncryptSensitiveWithUserKey.
    protection = int(attr("ProtectionLevel") or 1)

    fmt = None
    for prop in root.findall("DTS:Property", _NS):
        if prop.get(f"{{{DTS}}}Name") == "PackageFormatVersion" and prop.text:
            fmt = int(prop.text)

    text = data.decode("utf-8", errors="replace")
    majors = [int(m) for m in _SSIS_ASSEMBLY.findall(text)]
    return PackageInfo(
        file_name=file_name,
        object_name=attr("ObjectName") or "",
        dtsid=attr("DTSID") or "",
        version_guid=attr("VersionGUID") or "",
        protection_level=protection,
        package_format_version=fmt,
        sha256=package_sha256(data),
        project_parameters=set(_PROJECT_PARAM.findall(text)),
        project_connection_managers=set(_PROJECT_CONMGR.findall(text)),
        max_assembly_major=max(majors) if majors else None,
    )


def has_sensitive_values(data: bytes) -> bool:
    """True if the package stores a sensitive value (encrypted or not) in its body."""
    root = ET.fromstring(data)
    for el in root.iter():
        if el.get(f"{{{DTS}}}Sensitive") == "1" and ((el.text or "").strip() or len(el)):
            return True
        if el.tag == f"{{{DTS}}}EncryptedData" or el.tag.endswith("}EncryptedData"):
            return True
    return False
