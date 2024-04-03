import stix2
import re
from pycti import OpenCTIConnectorHelper, Tool, CustomObservableUserAgent
from pydantic import BaseModel, field_validator
from typing import Any, Final, Literal, Sequence
from .utils import (
    filter_truthly,
    first_or_none,
    oneof,
    oneof_nonempty,
    allof_nonempty,
    ip_proto,
)
from enum import Enum
from ntpath import split

IPAddr = stix2.IPv4Address | stix2.IPv6Address
SCO = (
    stix2.Artifact
    | stix2.AutonomousSystem
    | stix2.Directory
    | stix2.DomainName
    | stix2.EmailAddress
    | stix2.EmailMessage
    | stix2.File
    | IPAddr
    | stix2.MACAddress
    | stix2.Mutex
    | stix2.NetworkTraffic
    | stix2.Process
    | stix2.Software
    | stix2.URL
    | stix2.UserAccount
    | stix2.WindowsRegistryKey
    | stix2.X509Certificate
)
SDO = (
    stix2.AttackPattern
    | stix2.Campaign
    | stix2.CourseOfAction
    | stix2.Grouping
    | stix2.Identity
    | stix2.Incident
    | stix2.Indicator
    | stix2.Infrastructure
    | stix2.IntrusionSet
    | stix2.Location
    | stix2.Malware
    | stix2.MalwareAnalysis
    | stix2.Note
    | stix2.ObservedData
    | stix2.Opinion
    | stix2.Report
    | stix2.ThreatActor
    | stix2.Tool
    | stix2.Vulnerability
)
SRO = stix2.Relationship | stix2.Sighting
STIXList = Sequence[SCO | SDO | SRO]
TLPLiteral = Literal[
    "TLP:CLEAR", "TLP:WHITE", "TLP:GREEN", "TLP:AMBER", "TLP:AMBER-STRICT", "TLP:RED"
]

DUMMY_INDICATOR_ID: Final[str] = "indicator--167565fe-69da-5e2f-a1c1-0542736f9f9a"


class StandardID:
    """
    A string-like type that validates against STIX [object-type]--[UUID]
    """

    def __init__(self, id: str):
        self._id = id
        if not re.match(
            r"^.+--[0-9a-f]{8}-[0-9a-f]{4}-[0-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
            self._id,
            re.IGNORECASE,
        ):
            raise ValueError(f"{self._id} is not a valid UUID")

    def _str__(self):
        return self._id


# TODO: return StandardID|None
def tlp_marking_from_string(tlp_string: str | None):
    """
    Map a TLP string to a corresponding marking definition, or None

    Any characters ut to and including ":" are stripped and case is ignored.
    """
    if tlp_string is None:
        return None

    match re.sub(r"^[^:]+:", "", tlp_string).lower():
        case "clear" | "white":
            return stix2.TLP_WHITE.id
        case "green":
            return stix2.TLP_GREEN.id
        case "amber":
            return stix2.TLP_AMBER.id
        case "amber+strict":
            return "marking-definition--826578e1-40ad-459f-bc73-ede076f81f37"
        case "red":
            return stix2.TLP_RED
        case "":
            return None
        case _:
            raise ValueError(f"{tlp_string} is not a valid marking definition")


def tlp_allowed(entity: dict, max_tlp: TLPLiteral) -> bool:
    """
    If the entity has a TLP marking definition, ensure it is within a maximum
    allowed TLP
    """
    # Not sure what the correct logic is if the entity has several TLP markings. I asumme all have to be within max:
    return all(
        OpenCTIConnectorHelper.check_max_tlp(tlp, max_tlp)
        for mdef in entity["objectMarking"]
        for tlp in (mdef["definition"],)
        if mdef["definition_type"] == "TLP"
    )


def entity_value(entity: dict) -> str | None:
    """
    Return an observable's (or vulnerability's) value
    """
    match entity["entity_type"]:
        case "StixFile" | "Artifact":
            name = oneof_nonempty("name", "x_opencti_additional_names", within=entity)
            if isinstance(name, list) and len(name):
                return name[0]
            else:
                return str(name) if name is not None else None
        case "Directory":
            return oneof("path", within=entity)
        case "Process":
            return oneof("pid", "commandLine", within=entity)
        case "Software" | "Windows-Registry-Value-Type":
            return oneof("name", within=entity)
        case "User-Account":
            return oneof_nonempty(
                "account_login", "user_id", "display_name", within=entity
            )
        case "Vulnerability":
            return oneof("name", within=entity)
        case "Windows-Registry-Key":
            return oneof("key", within=entity)
        case _:
            return oneof("value", within=entity)


def entity_values(entity: dict) -> list[Any]:
    """
    Return an observable's (or vulnerability's) values
    """
    match entity["entity_type"]:
        case "StixFile" | "Artifact":
            return allof_nonempty("name", "x_opencti_additional_names", within=entity)
        case "Directory":
            return allof_nonempty("path", within=entity)
        case "Process":
            return allof_nonempty("pid", "commandLine", within=entity)
        case "Software" | "Windows-Registry-Value-Type":
            return allof_nonempty("name", within=entity)
        case "User-Account":
            return allof_nonempty(
                "account_login", "user_id", "display_name", within=entity
            )
        case "Vulnerability":
            return allof_nonempty("name", within=entity)
        case "Windows-Registry-Key":
            return allof_nonempty("key", within=entity)
        case _:
            return allof_nonempty("value", within=entity)


def entity_name_value(entity: dict):
    """
    Return the name and value of an entity, space separated
    """
    return " ".join(filter(None, [entity["entity_type"], entity_value(entity)]))


def incident_entity_relation_type(entity: dict):
    """
    Return the expected relationship type for the entity in the incident
    """
    match entity["entity_type"]:
        case "Vulnerability":
            return "targets"
        case _:
            return "related-to"


def add_refs_to_note(note: stix2.Note, objs: STIXList) -> stix2.Note:
    # Don't use new_version(), because that requires a new modified
    # timestamp (which must be newer than created):
    return stix2.Note(
        **{prop: getattr(note, prop) for prop in note if prop != "object_refs"},
        object_refs=list(set(note.object_refs) | {obj.id for obj in objs}),
    )


def add_incidents_to_note_refs(bundle: STIXList) -> STIXList:
    return [
        add_refs_to_note(obj, incidents) if isinstance(obj, stix2.Note) else obj
        for incidents in ([obj for obj in bundle if isinstance(obj, stix2.Incident)],)
        for obj in bundle
    ]


class FilenameBehaviour(Enum):
    CreateDir = "create-dir"
    RemovePath = "remove-path"


class StixHelper(BaseModel):
    """
    Helper class to simplify creation of STIX entities
    """

    common_properties: dict[str, Any] = {}
    sco_labels: list[str] = []
    filename_behaviour: set[FilenameBehaviour] = {FilenameBehaviour.CreateDir}

    @field_validator("filename_behaviour", mode="before")
    @classmethod
    def parse_behaviour_string(cls, behaviour):
        if isinstance(behaviour, str):
            if not behaviour:
                return set()
            # If this is a string, parse it as a comma-separated string with
            # enum values:
            return {string for string in behaviour.split(",")}
        else:
            # Otherwise, let pydantic validate whatever it is:
            return behaviour

    def create_tool(self, name: str):
        return stix2.Tool(
            id=Tool.generate_id(name),
            name=name,
            allow_custom=True,
            **self.common_properties,
        )

    def create_file(
        self, names: list[str], *, sha256: str | None = None, **properties
    ) -> list[stix2.Directory | stix2.File]:
        """
        Create a STIX file

        If sha256 is non-empty, it will be inserted into a hash object. If
        names contain more than one string, the first name will be used as
        "name", and the rest will be used as x_opencti_additional_names.

        If filename_behaviour contains CreateDir, a Directory object is created
        and referenced in parent_directory_ref. The path is extracted from the
        one of the filenames that contains a path. If filename_behaviour
        contains RemovePath, the path component of filenames will be removed.

        Examples:
        >>> h = StixHelper(filename_behaviour='')
        >>> h.create_file(names=['filename1', 'filename2'])
        [File(type='file', spec_version='2.1', id='file--f83c036d-56f6-5246-8585-1616d42c7669', name='filename1', defanged=False, x_opencti_additional_names=['filename2'])]
        >>> h.create_file(names=['/tmp/filename1', '/filename2'])
        [File(type='file', spec_version='2.1', id='file--09765542-1408-5026-8674-8128438fc940', name='/tmp/filename1', defanged=False, x_opencti_additional_names=['/filename2'])]
        >>> h = StixHelper(filename_behaviour='create-dir')
        >>> h.create_file(names=['/tmp/filename1', '/home/foo/Downloads/filename2'])
        [Directory(type='directory', spec_version='2.1', id='directory--b7ed5105-3a80-559d-9bd6-ec208b6d813e', path='/home/foo/Downloads', defanged=False), File(type='file', spec_version='2.1', id='file--ed282b5e-3ebe-5d5f-81e3-d52b629abb46', name='/tmp/filename1', parent_directory_ref='directory--b7ed5105-3a80-559d-9bd6-ec208b6d813e', defanged=False, x_opencti_additional_names=['/home/foo/Downloads/filename2'])]
        >>> h = StixHelper(filename_behaviour='create-dir,remove-path')
        >>> h.create_file(names=['filename1', '/home/foo/Downloads/filename2'])
        [Directory(type='directory', spec_version='2.1', id='directory--b7ed5105-3a80-559d-9bd6-ec208b6d813e', path='/home/foo/Downloads', defanged=False), File(type='file', spec_version='2.1', id='file--901c064f-7d08-5092-b84e-851f68c67a73', name='filename1', parent_directory_ref='directory--b7ed5105-3a80-559d-9bd6-ec208b6d813e', defanged=False, x_opencti_additional_names=['filename2'])]
        """
        path_names = {
            (path, filename) for name in names for path, filename in (split(name),)
        }
        # Sort the names in order to be able to test the function (otherwise
        # the order in the set will produce inconsistent results in doctest):
        paths = list(
            filter(lambda x: x, sorted({path_name[0] for path_name in path_names}))
        )
        filenames = list(
            filter(lambda x: x, sorted({path_name[1] for path_name in path_names}))
        )
        main_name = first_or_none(
            filenames
            if FilenameBehaviour.RemovePath in self.filename_behaviour
            else names
        )
        extra_names = (
            filenames[1:]
            if FilenameBehaviour.RemovePath in self.filename_behaviour
            else names[1:]
        )
        dir = None
        if paths and FilenameBehaviour.CreateDir in self.filename_behaviour:
            dir = stix2.Directory(
                path=paths[0], allow_custom=True, **self.common_properties
            )

        return filter_truthly(dir) + [
            stix2.File(
                name=main_name,
                hash={"SHA-256": sha256} if sha256 else None,
                parent_directory_ref=dir,
                allow_custom=True,
                **self.common_properties,
                x_opencti_additional_names=extra_names,
                **properties,
            )
        ]

    def create_addr_sco(self, address: str, **properties):
        """
        Create either an IPv4Address or IPv6Address, depending on the address
        type
        """
        match ip_proto(address):
            case "ipv4":
                SCO = stix2.IPv4Address
            case "ipv6":
                SCO = stix2.IPv6Address
            case _:
                raise ValueError(f"{address} is not a valid IP address")

        return SCO(
            value=address,
            allow_custom=True,
            **self.common_properties,
            labels=self.sco_labels,
            **properties,
        )

    def create_sco(self, type: str, value: str, **properties):
        """
        Create a SCO from its type name and properties
        """
        common_attrs = {
            "allow_custom": True,
            **self.common_properties,
            "labels": self.sco_labels,
        }
        match type:
            case "Directory":
                return stix2.Directory(path=value, **common_attrs, **properties)
            case "Domain-Name":
                return stix2.DomainName(value=value, **common_attrs, **properties)
            case "Email-Addr":
                return stix2.EmailAddress(value=value, **common_attrs, **properties)
            case "IPv4-Addr":
                return stix2.IPv4Address(value=value, **common_attrs, **properties)
            case "IPv6-Addr":
                return stix2.IPv6Address(value=value, **common_attrs, **properties)
            case "Mac-Addr":
                return stix2.MACAddress(value=value, **common_attrs, **properties)
            case "Process":
                return stix2.Process(pid=value, **common_attrs, **properties)
            case "Url":
                return stix2.URL(value=value, **common_attrs, **properties)
            case "User-Account":
                return self.create_account_from_username(value, **properties)
            case "StixFile":
                return stix2.File(name=value, **common_attrs, **properties)
            case "User-Agent":
                return CustomObservableUserAgent(
                    value=value, **common_attrs, **properties
                )
            case "Windows-Registry-Key":
                return stix2.WindowsRegistryKey(key=value, **common_attrs, **properties)
            case _:
                raise ValueError(f"Enrichment SCO {type} not supported")

    def create_account_from_username(self, username: str, **stix_properties):
        """
        Create a User-Account from a string that may container a username or
        both a username and a user ID

        If the username is of the form "name(uid=digits)", the uid is extracted
        and the resulting UserAccount will have both account_login and user_id
        set, otherwise account_login will be used.
        """
        uid = None
        # Some logs provide a username that also consists of a UID in parenthesis:
        if match := re.match(r"^(?P<name>[^\(]+)\(uid=(?P<uid>\d+)\)$", username or ""):
            uid = int(match.group("uid"))
            username = match.group("name")
        #
        # TODO: what about DOMAIN\username? set account_type = windows-domain

        return stix2.UserAccount(
            account_login=username,
            user_id=uid,
            allow_custom=True,
            **self.common_properties,
            lables=self.sco_labels,
            **stix_properties,
        )

    ## TODO: Revisit the usefulness of replacing files. What about all the refs
    ## created?
    # def aggregate_files(self, bundle: STIXList) -> STIXList:
    #    files: dict[Annotated[str, "SHA-256"], set[Annotated[str, "Filenames"]]] = {
    #        file.hashes["SHA-256"]: {
    #            file2.name
    #            for file2 in files
    #            if compare_field(file, file2, "hashes.SHA-256")
    #        }
    #        for files in ([obj for obj in bundle if isinstance(obj, stix2.File)],)
    #        for file in files
    #        if "hashes" in file and "SHA-256" in file.hashes
    #    }

    #    return [self.create_file(list(names), hash) for hash, names in files.items()]
