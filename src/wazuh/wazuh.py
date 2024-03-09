import json
import stix2
import yaml
import dateparser
import re
import bisect
import ipaddress
from .opensearch import OpenSearchClient
from pathlib import Path
from pycti import (
    AttackPattern,
    CustomObservableHostname,
    Identity,
    Incident,
    Note,
    OpenCTIConnectorHelper,
    OpenCTIMetricHandler,
    StixCoreRelationship,
    StixSightingRelationship,
    Tool,
    get_config_variable,
)
from typing import Final
from hashlib import sha256
from datetime import datetime
from urllib.parse import urljoin
from os.path import basename, commonprefix
from pydantic import BaseModel
from functools import cache

# TODO: find related observables for sighting and operate on those (match against pattern?, what does based-on mean?)

#
# Populate agents using agent metadata?
# SETTING: agent_labels: comma-separated list of labels to attach to agents
# SETTING: siem_labels: comma-separated list of labels to attach to wazuh identity
# Search include/exclue in setting level (add wazuh-opencti as default)
# Create wazuh integration that creates incidents in opencti
# get_config_variable with required doesn't throw if not set


# UUID_RE = r"^a[0-9a-f]{8}-[0-9a-f]{4}-[0-5][0-9a-f]{3}-[089ab][0-9a-f]{3}-[0-9a-f]{12}$"
# STIX_ID_REGEX = re.compile(f".+--{UUID_RE}", re.IGNORECASE)

# TODO: create indicator sighting? ask filigran?


# TODO: Improve logic, avoid all recalculations (unless cache fixes this?)
class SightingsCollector:
    """
    Helper module to reduce the number of sightings to one instance per SDO

    When a sighting is added using add(), the metadata passed to the function
    is added to a dict. Any subsequent calls for the same sighter_id updates
    first_seen, last_seen and count accordingly.
    """

    class Meta(BaseModel):
        observable_id: str
        sighter_name: str
        first_seen: str
        last_seen: str
        count: int
        alerts: dict[str, list[dict]]
        max_rule_level: int = 0

    def __init__(self, *, observable_id: str):
        self._sightings: dict[str, SightingsCollector.Meta] = {}
        # This module will only be used for one SCO at a time:
        self._observable_id = observable_id
        self._latest = ""

    @cache
    def _alerts_timestamps_sorted(self, rule_id: str):
        return [
            alert["_source"]["@timestamp"]
            for sighting in self._sightings.values()
            for alert in sighting.alerts.get(rule_id, [])
        ]

    def add(self, *, timestamp: str, sighter: stix2.Identity, alert: dict):
        """
        Add or update metadata for sightings of an observable in sighter_id
        """
        rule_id = alert["_source"]["rule"]["id"]
        if sighter.id in self._sightings:
            self._sightings[sighter.id].first_seen = min(
                self._sightings[sighter.id].first_seen, timestamp
            )
            self._sightings[sighter.id].last_seen = max(
                self._sightings[sighter.id].last_seen, timestamp
            )
            self._sightings[sighter.id].count += 1
            if rule_id in self._sightings[sighter.id].alerts:
                bisect.insort(
                    self._sightings[sighter.id].alerts[rule_id],
                    alert,
                    key=lambda a: a["_source"]["@timestamp"],
                )
            else:
                self._sightings[sighter.id].alerts[rule_id] = [alert]

            if timestamp > self._latest:
                self._latest = timestamp

            self._sightings[sighter.id].max_rule_level = max(
                [self._sightings[sighter.id].max_rule_level]
                + [
                    alert["_source"]["rule"]["level"]
                    for alerts in self._sightings[sighter.id].alerts.values()
                    for alert in alerts
                ]
            )
        else:
            self._sightings[sighter.id] = SightingsCollector.Meta(
                observable_id=self._observable_id,
                sighter_name=sighter.name,
                first_seen=timestamp,
                last_seen=timestamp,
                count=1,
                alerts={rule_id: [alert]},
            )
            self._latest = timestamp

    def observable_id(self):
        return self._observable_id

    def collated(self):
        return self._sightings

    def last_sighting_timestamp(self):
        return self._latest

    @cache
    def max_rule_level(self):
        return max([sighting.max_rule_level for sighting in self._sightings.values()])

    @cache
    def first_seen(self, rule_id: str | None = None):
        if rule_id is None:
            return min([sighting.first_seen for sighting in self._sightings.values()])
        else:
            return min(self._alerts_timestamps_sorted(rule_id))

    @cache
    def last_seen(self, rule_id: str | None = None):
        if rule_id is None:
            return max([sighting.last_seen for sighting in self._sightings.values()])
        else:
            return max(self._alerts_timestamps_sorted(rule_id))

    @cache
    def alerts_by_rule_id(self):
        """
        Return a dict with alerts grouped by rule_id

        The keys are Wazuh rule IDs as strings (since they are strings in Wazuh). The values are arrays of dicts, containing all alerts with that rule ID.
        Example: { "1234": [{…}, {…}] "1235": […] }
        """
        return {
            rule_id: sorted(
                alerts,
                key=lambda a: a["_source"]["@timestamp"],
            )
            for rule_id in {
                rule_id
                for sighting in self._sightings.values()
                for rule_id in sighting.alerts
            }
            for alerts in (
                [
                    alert
                    for alerts in [
                        sighting.alerts[rule_id]
                        for sighting in self._sightings.values()
                        if rule_id in sighting.alerts
                    ]
                    for alert in alerts
                ],
            )
        }

    @cache
    def alerts_by_rule_meta(self):
        return {
            rule_id: {
                "alerts": sorted(
                    alerts,
                    key=lambda a: a["_source"]["@timestamp"],
                ),
                "first_seen": min([alert["_source"]["@timestamp"] for alert in alerts]),
                "last_seen": max([alert["_source"]["@timestamp"] for alert in alerts]),
                "sighters": [
                    sighter
                    for sighter, sighting in self._sightings.items()
                    if rule_id in sighting.alerts
                ],
            }
            for rule_id in {
                rule_id
                for sighting in self._sightings.values()
                for rule_id in sighting.alerts
            }
            for alerts in (
                [
                    alert
                    for alerts in [
                        sighting.alerts[rule_id]
                        for sighting in self._sightings.values()
                        if rule_id in sighting.alerts
                    ]
                    for alert in alerts
                ],
            )
        }

    @cache
    def alerts_by_sighter_meta(self):
        return {
            sighter_id: {
                "alerts": sorted(
                    [alert for alerts in meta.alerts.values() for alert in alerts],
                    key=lambda a: a["_source"]["@timestamp"],
                ),
                "sighter_name": meta.sighter_name,
            }
            for sighter_id, meta in self._sightings.items()
        }

    @cache
    def alerts(self):
        return [
            alert
            for sighting in self._sightings.values()
            for alerts in sighting.alerts.values()
            for alert in alerts
        ]


def has(obj: dict, spec: list[str], value=None):
    """
    Test whether obj contains a specific structure

    Examples:
    `obj = {"a": {"b": 42}`
    `has(obj, ['a'])` returns true
    `has(obj, ['b'])` returns false
    `has(obj, ['a', 'b'])` returns true
    `has(obj, ['a', 'b'], 43)` returns false
    `has(obj, ['a', 'b'], 42)` returns true
    """
    if not spec:
        return obj == value if value is not None else True
    try:
        key, *rest = spec
        return has(obj[key], rest, value=value)
    except (KeyError, TypeError):
        return False


# def has_any_val(obj:dict, spec:list[str], values:list|None = None):
#   if not spec:
#       return any(obj == value for value in values) if values is not None else True
#   try:
#       key, *rest = spec
#       return has_any_val(obj[key], rest, values=values)
#   except (KeyError, TypeError):
#       return False


def has_any(obj: dict, spec1: list[str], spec2: list[str]):
    """
    Test whether an object contains a specific structure

    Test whether spec1 contains a specific structure (a "JSON path"). Then, test whether the resulting object has any of the keys listed in spec2. Example:

    `has_any({"a": {"b": {"d": 1, "e": 2}}}, ["a", "b"], ["c", "d"])` returns true, because "b" exists in "a", and "a" exists in obj, and either "c" or "d" exists in "b".
    """
    if not spec1:
        return any(key in obj for key in spec2)
    try:
        key, *rest = spec1
        return has_any(obj[key], rest, spec2)
    except (KeyError, TypeError):
        return False


def parse_config_datetime(value, setting_name):
    if value is None:
        return None

    timestamp = dateparser.parse(value)
    if not timestamp:
        raise ValueError(
            f'The config variable "{setting_name}" datetime expression cannot be parsed: "{value}"'
        )

    return timestamp


def parse_match_patterns(patterns: str):
    """
    Parse a string like "foo=bar,baz=qux" into
    [{"match": {"foo": "bar"}}, {"match": {"baz": "qux"}}]

    Parameters
    ----------
    patterns : str
               String of key=value pairs separated by comma
    """
    if patterns is None:
        return None

    pairs = [pattern.split("=") for pattern in patterns.split(",")]
    if any(len(pair) != 2 for pair in pairs):
        raise ValueError(f'The match patterns "{patterns}" is invalid')

    return [{"match": {pair[0]: pair[1]}} for pair in pairs]


def to_tlp(tlp_string):
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


def tlp_allowed(entity, max_tlp):
    # Not sure what the correct logic is if the entity has several TLP markings. I asumme all have to be within max:
    return all(
        OpenCTIConnectorHelper.check_max_tlp(tlp, max_tlp)
        for mdef in entity["objectMarking"]
        for tlp in (mdef["definition"],)
        if mdef["definition_type"] == "TLP"
    )


def rule_level_to_severity(level: int):
    match level:
        case level if level in range(7, 10):
            return "medium"
        case level if level in range(11, 13):
            return "high"
        case level if level in range(14, 15):
            return "critical"
        case _:
            return "low"


def cvss3_to_severity(score: float):
    match score:
        case score if score > 9.0:
            return "critical"
        case score if score > 7.0:
            return "high"
        case score if score > 4.0:
            return "medium"
        case _:
            return "low"


def alert_md_table(alert: dict, additional_rows: list[tuple[str, str]] = []):
    s = alert["_source"]
    return (
        "|Key|Value|\n"
        "|---|---|\n"
        f"|Rule ID|{s['rule']['id']}|\n"
        f"|Rule desc.|{s['rule']['description']}|\n"
        f"|Rule level|{s['rule']['level']}|\n"
        f"|Alert ID|{alert['_id']}/{s['id']}|\n"
    ) + "".join(f"|{key}|{value}|\n" for key, value in additional_rows)


def oneof(*keys: str, within: dict, default=None):
    return next((within[key] for key in keys if key in within), default)


def entity_name_value(entity: dict):
    name = entity["entity_type"]
    value = None
    match name:
        case "StixFile" | "Artifact":
            value = oneof("name", "x_opencti_additional_names", within=entity)
        case "Directory":
            value = oneof("path", "x_opencti_additional_names", within=entity)
        case "Software" | "Windows-Registry-Value-Type":
            value = oneof("name", within=entity)
        case "User-Account":
            value = oneof("account_login", "user_id", "display_name", within=entity)
        case "Vulnerability":
            value = oneof("name", within=entity)
        case "Windows-Registry-Key":
            value = oneof("key", within=entity)
        case _:
            value = oneof("value", within=entity)

    return " ".join(filter(None, [name, value]))


def common_prefix_string(strings: list[str], elideString: str = "[…]"):
    if not strings:
        return ""
    if len(common := commonprefix(strings)) == len(strings[0]):
        return common
    else:
        return common + elideString


def create_tool_stix(name: str):
    return stix2.Tool(
        id=Tool.generate_id(name),
        name=name,
    )


def incident_entity_relation_type(entity: dict):
    match entity["entity_type"]:
        case "Vulnerability":
            return "targets"
        case _:
            return "related-to"


class WazuhConnector:
    class MetricHelper:
        def __init__(self, metric: OpenCTIMetricHandler):
            self.metric = metric

        def __enter__(self):
            self.metric.inc("run_count")
            self.metric.state("running")
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            self.metric.state("idle")
            if exc_type is not None:
                self.metric.inc("client_error_count")

    def __init__(self):
        self.CONNECTOR_VERSION: Final[str] = "0.0.1"
        # TODO: it appears that the dummy indicator has to exist for external references to work:
        self.DUMMY_INDICATOR_ID: Final[
            str
            # ] = "indicator--220d5816-3786-5421-a6d3-fb149a0df54e"  # "indicator--c1034564-a9fb-429b-a1c1-c80116cc8e1e"
        ] = "indicator--167565fe-69da-5e2f-a1c1-0542736f9f9a"  # = "indicator--1195bcd2-67ee-563a-83f8-29ebd9eacec7"

        config_file_path = Path(__file__).parent.parent.resolve() / "config.yml"
        config = (
            yaml.load(open(config_file_path, encoding="utf-8"), Loader=yaml.SafeLoader)
            if config_file_path.is_file()
            else {}
        )

        self.helper = OpenCTIConnectorHelper(config, True)
        self.confidence = (
            int(self.helper.connect_confidence_level)
            if isinstance(self.helper.connect_confidence_level, int)
            else None
        )
        # TODO: for config, consider using pydanic and BaseSettings. Look at https://github.com/OpenCTI-Platform/connectors/blob/abf07fb6bd423c104a10207626520c2836d7e586/internal-enrichment/shodan-internetdb/src/shodan_internetdb/config.py#L26. If not, ensure empty values in required throws
        self.max_tlp = (
            re.sub(r"^(tlp:)?", "TLP:", tlp, flags=re.IGNORECASE).upper()
            if isinstance(
                tlp := get_config_variable(
                    "WAZUH_MAX_TLP", ["wazuh", "max_tlp"], config, required=True
                ),
                str,
            )
            else None
        )
        self.tlps = (
            [tlp]
            if (
                tlp := to_tlp(
                    get_config_variable(
                        "WAZUH_TLP", ["wazuh", "tlp"], config, required=True
                    )
                )
            )
            is not None
            else []
        )
        self.hits_limit = get_config_variable(
            "WAZUH_MAX_HITS", ["wazuh", "max_hits"], config, isNumber=True, default=10
        )
        self.system_name = get_config_variable(
            "WAZUH_SYSTEM_NAME", ["wazuh", "system_name"], config, default="Wazuh SIEM"
        )
        self.agents_as_systems = get_config_variable(
            "WAZUH_AGENTS_AS_SYSTEMS",
            ["wazuh", "agents_as_systems"],
            config,
            default=True,
        )
        self.alerts_as_notes = get_config_variable(
            "WAZUH_ALERTS_AS_NOTES",
            ["wazuh", "alerts_as_notes"],
            config,
            default=True,
        )
        self.create_obs_note = get_config_variable(
            "WAZUH_CREATE_OBSERVABLE_NOTE",
            ["wazuh", "create_observable_note"],
            config,
            default=True,
        )
        self.search_agent_ip = get_config_variable(
            "WAZUH_SEARCH_AGENT_IP",
            ["wazuh", "search_agent_ip"],
            config,
            default=False,
        )
        self.search_agent_name = get_config_variable(
            "WAZUH_SEARCH_AGENT_NAME",
            ["wazuh", "search_agent_name"],
            config,
            default=False,
        )
        self.search_after = parse_config_datetime(
            value=get_config_variable(
                "WAZUH_SEARCH_ONLY_AFTER", ["wazuh", "search_only_after"], config
            ),
            setting_name="search_only_after",
        )
        self.search_include = get_config_variable(
            "WAZUH_SEARCH_INCLUDE_MATCH", ["wazuh", "search_include_match"], config
        )
        self.search_exclude = get_config_variable(
            "WAZUH_SEARCH_EXCLUDE_MATCH",
            ["wazuh", "search_exclude_match"],
            config,
            default="data.integration=opencti",
        )
        self.create_obs_sightings = get_config_variable(
            "WAZUH_CREATE_OBSERVABLE_SIGHTINGS",
            ["wazuh", "create_observable_sightings"],
            config,
            default=True,
        )
        self.max_extrefs = (
            maxrefs
            if isinstance(
                maxrefs := get_config_variable(
                    "WAZUH_SIGHTING_MAX_EXTREFS",
                    ["wazuh", "sighting_max_extrefs"],
                    config,
                    isNumber=True,
                    default=10,
                ),
                int,
            )
            else 10
        )
        self.max_extrefs_per_alert_rule = (
            maxrefs
            if isinstance(
                maxrefs := get_config_variable(
                    "WAZUH_SIGHTING_MAX_EXTREFS_PER_ALERT_RULE",
                    ["wazuh", "sighting_max_extrefs_per_alert_rule"],
                    config,
                    isNumber=True,
                    default=1,
                ),
                int,
            )
            else 1
        )
        self.max_notes = (
            maxrefs
            if isinstance(
                maxrefs := get_config_variable(
                    "WAZUH_SIGHTING_MAX_NOTES",
                    ["wazuh", "sighting_max_notes"],
                    config,
                    isNumber=True,
                    default=10,
                ),
                int,
            )
            else 10
        )
        self.max_notes_per_alert_rule = (
            maxrefs
            if isinstance(
                maxrefs := get_config_variable(
                    "WAZUH_SIGHTING_MAX_NOTES_PER_ALERT_RULE",
                    ["wazuh", "sighting_max_notes_per_alert_rule"],
                    config,
                    isNumber=True,
                    default=1,
                ),
                int,
            )
            else 1
        )
        self.create_sighting_summary = get_config_variable(
            "WAZUH_SIGHTING_SUMMARY_NOTE",
            ["wazuh", "sighting_summary_note"],
            config,
            default=True,
        )

        self.require_indicator_for_incidents = get_config_variable(
            "WAZUH_INCIDENT_REQUIRE_INDICATOR",
            ["wazuh", "incident_require_indicator"],
            config,
            default=True,
        )
        # TODO: verify modes using Field:
        self.create_incident = get_config_variable(
            "WAZUH_INCIDENT_CREATE_MODE",
            ["wazuh", "incident_create_mode"],
            config,
            default="per_sighting",
        )
        self.create_agent_ip_obs = get_config_variable(
            "WAZUH_CREATE_AGENT_IP_OBSERVABLE",
            ["wazuh", "create_agent_ip_observable"],
            config,
            default=False,
        )
        self.create_agent_host_obs = get_config_variable(
            "WAZUH_CREATE_AGENT_HOSTNAME_OBSERVABLE",
            ["wazuh", "create_agent_hostname_observable"],
            config,
            default=False,
        )
        # TODO: bundle_size_abort_limit
        # TODO: hit_abort_limit: abort if met
        # Ignore local addresses
        # Ignore too wide dirs? "/", "/usr" etc.

        self.stix_common_attrs = {
            "object_marking_refs": self.tlps,
            "confidence": self.confidence,
        }
        # Add moe useful meta to author?
        self.author = stix2.Identity(
            id=Identity.generate_id("Wazuh", "organization"),
            **self.stix_common_attrs,
            name="Wazuh",
            identity_class="organization",
            description="Wazuh",
        )
        self.stix_common_attrs["created_by_ref"] = self.author["id"]
        self.siem_system = stix2.Identity(
            id=Identity.generate_id(self.system_name, "system"),
            **self.stix_common_attrs,
            name=self.system_name,
            identity_class="system",
        )
        self.app_url = get_config_variable(
            "WAZUH_APP_URL", ["wazuh", "app_url"], config, required=True
        )
        self.client = OpenSearchClient(
            helper=self.helper,
            url=get_config_variable(  # type: ignore
                "WAZUH_OPENSEARCH_URL",
                ["wazuh", "opensearch_url"],
                config,
                required=True,
            ),
            username=get_config_variable(  # type: ignore
                "WAZUH_USERNAME", ["wazuh", "username"], config, required=True
            ),
            password=get_config_variable(  # type: ignore
                "WAZUH_PASSWORD", ["wazuh", "password"], config, required=True
            ),
            limit=self.hits_limit if isinstance(self.hits_limit, int) else 10,
            index=get_config_variable(  # type: ignore
                "WAZUH_INDEX", ["wazuh", "index"], config, default="wazuh-alerts-*"
            ),
            search_after=self.search_after,
            include_match=parse_match_patterns(self.search_include),  # type: ignore
            exclude_match=parse_match_patterns(self.search_exclude),  # type: ignore
            # TODO: optional sort on rule.level before time?
            # TODO: Use Field to validate. Document that search_after is prepended if defined:
            # filters=[{"range": {"rule.level": {"gte": 8}}}],
            # filters=get_config_variable("WAZUH_SEARCH_FILTER", ["wazuh", "search_filter"], config),
        )

    def start(self):
        self.helper.metric.state("idle")
        self.helper.listen(self.process_message)

    def process_message(self, data):
        # Use a helper class that ensures to always updates the running state of the connector, as well as incrementing the error count on uncaught exceptions:
        with self.MetricHelper(self.helper.metric):
            return self._process_message(data)

    def _process_message(self, data):
        entity = None
        entity_type = "observable"
        if data["entity_id"].startswith("vulnerability--"):
            entity = self.helper.api.vulnerability.read(id=data["entity_id"])
            entity_type = "vulnerability"
            # It doesn't make sense to support indicators, having to parse all sorts of patterns:
        # elif data["entity_id"].startswith("indicator--"):
        #    entity = self.helper.api.indicator.read(id=data["entity_id"])
        else:
            entity = self.helper.api.stix_cyber_observable.read(id=data["entity_id"])

        if entity is None:
            raise ValueError("Entity/observable not found")

        # Remove:
        self.helper.log_debug(f"ENTITY: {entity}")

        if not tlp_allowed(entity, self.max_tlp):
            self.helper.connector_logger.info(f"max tlp: {self.max_tlp}")
            raise ValueError("Entity ignored because TLP not allowed")

        # Figure out exactly what this does (change id format?);
        enrichment = self.helper.get_data_from_enrichment(data, entity)
        stix_entity = enrichment["stix_entity"]
        # Remove:
        self.helper.log_debug(f"STIX_ENTITY: {stix_entity}")

        # Remove:
        if entity["entity_type"] != stix_entity["x_opencti_type"]:
            self.helper.log_debug(
                f'DIFFERENT: entity_type: {entity["entity_type"]}, x_opencti_type: {stix_entity["x_opencti_type"]}'
            )

        obs_indicators = self.entity_indicators(entity)
        # Remove:
        self.helper.log_debug(f"INDS: {obs_indicators}")

        if (
            entity_type == "observable"
            and not obs_indicators
            and not self.create_obs_sightings
        ):
            self.helper.connector_logger.info(
                "Observable has no indicators and WAZUH_CREATE_OBSERVABLE_SIGHTINGS is false"
            )
            return "Observable has no indicators"

        result = self._query_alerts(entity, stix_entity)
        if result is None:
            # Even though the entity is supported (an exception is throuwn
            # otherwise), not all entities contains information that is
            # searchable in Wazuh. There may also not be enough information to
            # perform a search that is targeted enough. This is not an error:
            return f"{entity['entity_type']} has no queryable data"

        hits = result["hits"]["hits"]
        # TODO: create note (optionally) if no hits found(?):
        if not hits:
            return "No hits found"

        # The sigher is the Wazuh SIEM identity unless later overriden by
        # agents_as_systems:
        sighter = self.siem_system
        # Use a helper module to create as few sighting objects as possible,
        # and modify their first_seen, last_seen and count instead:
        sightings_collector = SightingsCollector(observable_id=entity["standard_id"])
        agents = {}
        # The complete STIX bundle to send:
        bundle = [self.author, self.siem_system]
        for hit in hits:
            try:
                s = hit["_source"]
                if (
                    has(s, ["agent", "id"])
                    and self.agents_as_systems
                    # Do not create systems for master/worker, use the Wazuh system instead:
                    and int(s["agent"]["id"]) > 0
                ):
                    agents[s["agent"]["id"]] = sighter = self.create_agent_stix(hit)

                sightings_collector.add(
                    timestamp=s["@timestamp"],
                    sighter=sighter,
                    alert=hit,
                )

            except (IndexError, KeyError):
                raise OpenSearchClient.ParseError(
                    "Failed to parse _source: Unexpected JSON structure"
                )

        # TODO: Use in incident and add as targets(?):
        if self.create_agent_ip_obs:
            bundle += self.create_agent_addr_obs(alerts=hits)
        if self.create_agent_host_obs:
            bundle += self.create_agent_hostname_obs(alerts=hits)

        # FIXME: WAZUH_INCIDENT_CREATE_MODE=per_sighting produces missing ref
        # errors unless dummy indicator exists. Update: might be random and
        # unrelated.
        sighting_ids = []
        for sighter_id, meta in sightings_collector.collated().items():
            sighting = self.create_sighting_stix(sighter_id=sighter_id, metadata=meta)
            sighting_ids.append(sighting.id)
            bundle += [sighting] + self.create_sighting_alert_notes(
                sighting_id=sighting.id, metadata=meta
            )

        ###############
        # hostname seems to be the target, not a system
        # relation "uses" on attack pattern (mitre)
        #
        # Issues:
        #   When creating alerts, double alerts is an issue when rule engine is enabled
        #   The indicator is not available yet when working with the observable (timing issue)
        # Setting: incident for sightings in obs
        # Setting: Incident for sightings in obs with indicator
        # Setting: One incident per alert rule.id
        # Setting: Include rule ids, exclude rule ids
        # Setting: Agents as hostnames
        # Setting: Agent IP as observable
        # Setting: max_ext_ref per rule_id, per search?. same for note
        # Setting for limiting notes per sighting (0 disables notes for sightings)
        # Setting for limiting ext.refs. per sighting (0 disables)
        # Setting for adhering to detection, valid_until, min score(?)
        #
        # Create external reference to wazuh with the query that was ran (discover? custom columns?)
        # Look into how playbooks can be used
        # Add mitre connector and import tactics etc.
        # Look through wazuh rules to find occurances of usernames, addresses etc.
        ###############

        # Group alerts by ID and see how many are unique. Setting: inncident per rule id?
        # Add an enrichment function that creates mitre, tools etc for either
        # when relevant fields are present or per rule id/group. Add a setting
        # for each one (yaml only?)?
        # tool:
        #   uses → attack pattern

        alerts_by_rule_id = sightings_collector.alerts_by_rule_id()
        counts = {rule_id: len(alerts) for rule_id, alerts in alerts_by_rule_id.items()}
        self.helper.log_debug(f"COUNTS: {counts}")

        if self.create_sighting_summary:
            bundle += [
                self.create_summary_note(
                    result=result,
                    sightings_meta=sightings_collector,
                    sightings_ids=sighting_ids,
                )
            ]

        # TODO: PER_SIGHTING, PER_ALERT, PER_ALERT_RULE. Add mitre, other
        # enrichments. Summary note. Add new first_seen/last_seen per rule id
        # in summary note. Ensure summary note is at top (latest_hit timetamp?)
        if (
            self.require_indicator_for_incidents
            and entity_type == "observable"
            and not obs_indicators
        ):
            self.helper.connector_logger.info(
                "Not creating incident because entity is an observable, an indicator is required and no indicators are found"
            )
        else:
            bundle += self.create_incidents(
                entity=entity,
                obs_indicators=obs_indicators,
                result=result,
                sightings_meta=sightings_collector,
            )

        bundle += list(agents.values())
        sent_count = len(
            self.helper.send_stix2_bundle(
                self.helper.stix2_create_bundle(bundle), update=True
            )
        )
        return f"Sent {sent_count} STIX bundle(s) for worker import"

    def entity_indicators(self, entity: dict) -> list[dict]:
        if "indicators" not in entity:
            return []
        return [
            ind
            for obj in entity["indicators"]
            if (ind := self.helper.api.indicator.read(id=obj["id"])) is not None
            if ind is not None
        ]

    # TODO: get help to write more targeted searches. If fields are used
    # differently by different decoders, search for that rule. Get help by
    # making issuers report their fields using something like 'GET
    # /wazuh-alerts-*/_field_caps?fields=*filename*'

    def _query_alerts(self, entity, stix_entity) -> dict | None:
        match entity["entity_type"]:
            # TODO: Use name as well as hash if defined (optional, config)
            # NOTE: Backslashes are execpted to not be escaped in OpenCTI
            case "StixFile" | "Artifact":
                if (
                    entity["entity_type"] == "StixFile"
                    and "name" in stix_entity
                    and not has_any(
                        stix_entity, ["hashes"], ["SHA-256", "SHA-1", "MD5"]
                    )
                ):
                    # size? use size too if so
                    filenames = list(
                        map(
                            lambda a: re.escape(a),
                            [stix_entity["name"]]
                            + stix_entity["x_opencti_additional_names"],
                        )
                    )
                    return self.client.search_multi_regex(
                        fields=[
                            "data.ChildPath",
                            "data.ParentPath",
                            "data.Path",
                            "data.TargetFileName",
                            "data.TargetPath",
                            "data.audit.file.name",
                            "data.file",
                            "data.sca.check.file",
                            "data.smbd.filename",
                            "data.smbd.new_filename",
                            "data.virustotal.source.file",
                            "data.win.eventdata.file",
                            "data.win.eventdata.filePath",
                            "syscheck.path",
                        ],
                        # Search for paths ignoring case for better experience
                        # on Windows:
                        case_insensitive=True,
                        regexp=f'.*[/\\\\]+({"|".join(filenames)})',
                    )
                elif has(stix_entity, ["hashes", "SHA-256"]):
                    return self.client.search_multi(
                        fields=["*sha256*"], value=stix_entity["hashes"]["SHA-256"]
                    )
                elif has(stix_entity, ["hashes", "SHA-1"]):
                    return self.client.search_multi(
                        fields=["*sha1*"], value=stix_entity["hashes"]["SHA-1"]
                    )
                else:
                    return None

            # TODO: add setting that only looks up public addresses? (GeoLocation.countr_name exists?) Tested, not consistent enough
            case "IPv4-Addr" | "IPv6-Addr":
                fields = [
                    "*.ip",
                    "*.IP",
                    "*.dest_ip",
                    "*.dstip",
                    "*.src_ip",
                    "*.srcip",
                    "*.ClientIP",
                    "*.ActorIpAddress",
                    "*.remote_ip",
                    "*.remote_ip_address",
                    "*.remote_address",
                    "*.destination_address",
                    "*.nat_destination_ip",
                    "*.sourceIPAddress",
                    "*.source_ip_address",
                    "*.source_address",
                    "*.local_address",
                    "*.LocalIp",
                    "*.nat_source_ip",
                    "*.callerIp",
                    "*.ipAddress",
                    "*.IPAddress",
                    "*.ipv*.address",
                    "data.win.eventdata.queryName",
                ]
                address = entity["observable_value"]
                if self.search_agent_ip:
                    return self.client.search_multi(
                        fields=fields,
                        value=address,
                    )
                else:
                    return self.client.search(
                        must={
                            "multi_match": {
                                "query": address,
                                "fields": fields,
                            }
                        },
                        must_not={"match": {"agent.ip": address}},
                    )
            case "Mac-Addr":
                return self.client.search_multi(
                    fields=[
                        "*.src_mac",
                        "*.srcmac",
                        "*.smac",
                        "*.dst_mac",
                        "*.dstmac",
                        "*.dmac",
                        "*.mac",
                        "data.osquery.columns.interface",
                    ],
                    value=entity["observable_value"],
                )
            case "Network-Traffic":
                query = []
                if "src_ref" in stix_entity:
                    src_ip = self.helper.api.stix_cyber_observable.read(
                        id=stix_entity["src_ref"]
                    )
                    if src_ip and "value" in src_ip:
                        query.append(
                            {
                                "multi_match": {
                                    "query": src_ip["value"],
                                    "fields": [
                                        "*.src_ip",
                                        "*.srcip",
                                        "*.local_address",
                                        "*.source_address",
                                        "*.nat_source_ip",
                                        "*.LocalIp",
                                    ],
                                }
                            }
                        )
                if "src_port" in stix_entity:
                    query.append(
                        {
                            "multi_match": {
                                "query": stix_entity["src_port"],
                                "fields": [
                                    "*.src_port",
                                    "*.srcport",
                                    "*.local_port",
                                    "*.spt",
                                    "*.nat_source_port",
                                    "data.IP",
                                ],
                            }
                        }
                    )
                if "dst_ref" in stix_entity:
                    dest_ip = self.helper.api.stix_cyber_observable.read(
                        id=stix_entity["dst_ref"]
                    )
                    if dest_ip and "value" in dest_ip:
                        query.append(
                            {
                                "multi_match": {
                                    "query": dest_ip["value"],
                                    "fields": [
                                        "*.dest_ip",
                                        "*.dstip",
                                        "*.remote_address",
                                        "*.destination_address",
                                        "*.nat_destination_ip",
                                    ],
                                }
                            }
                        )
                if "dst_port" in stix_entity:
                    query.append(
                        {
                            "multi_match": {
                                "query": stix_entity["dst_port"],
                                "fields": [
                                    "*.dest_port",
                                    "*.dstport",
                                    "*.remote_port",
                                    "*.dpt",
                                    "*.nat_destination_port",
                                ],
                            }
                        }
                    )

                if query:
                    return self.client.search(query)
                else:
                    return None
            case "Email-Addr":
                return self.client.search_multi(
                    fields=[
                        "*email",
                        "*Email",
                        "data.office365.UserId",
                    ],
                    value=stix_entity["account_login"],
                )
            case "Domain-Name" | "Hostname":
                fields = [
                    "data.win.eventdata.queryName",
                    "data.dns.question.name",
                    "*.hostname",
                    "*.domain",
                    "*.netbios_hostname",
                    "*.dns_hostname",
                    "*.HostName",
                    "*.host",
                ]
                hostname = entity["observable_value"]
                if self.search_agent_name:
                    return self.client.search_multi(
                        fields=fields,
                        value=hostname,
                    )
                else:
                    return self.client.search(
                        must={
                            "multi_match": {
                                "query": hostname,
                                "fields": fields,
                            }
                        },
                        # TODO: configurable?:
                        # data.audit.exe /usr/bin/ssh
                        # data.audit.execve.a* = hostname
                        # must_not={"match": {"predecoder.hostname": hostname}},
                    )
            case "Url":
                return self.client.search_multi(
                    fields=["*url", "*Url", "*.URL", "*.uri"],
                    value=entity["observable_value"],
                )
            case "Directory":
                # NOTE: Backslashes are execpted to not be escaped in OpenCTI
                path = stix_entity["path"].replace("\\", "\\\\\\\\")
                # Search for the directory path also in filename/path fields
                # that may be of intereset (not necessarily all the same fields
                # as in File/StixFile:
                filename_searches = [
                    {
                        "regexp": {
                            field: {
                                "value": f"{path}[/\\\\]+.*",
                                # Search for paths ignoring case for better
                                # experience on Windows:
                                "case_insensitive": True,
                            }
                        }
                    }
                    # Do not add globs here; it will throw:
                    for field in [
                        "data.ChildPath",
                        "data.ParentPath",
                        "data.Path",
                        "data.TargetPath",
                        "data.audit.file.name",
                        "data.smbd.filename",
                        "data.smbd.new_filename",
                        "data.win.eventdata.image",
                        "data.win.eventdata.sourceImage",
                        "data.win.eventdata.targetImage",
                    ]
                ]
                return self.client.search(
                    should=[
                        {
                            "multi_match": {
                                "query": path,
                                "fields": [
                                    "*.path",
                                    "*.pwd",
                                    "*.currentDirectory",
                                    "*.directory",
                                    "data.home",
                                    "data.SourceFilePath",
                                    "data.TargetPath",
                                ],
                            }
                        }
                    ]
                    + filename_searches
                )

            case "Windows-Registry-Key":
                return self.client.search_multi(
                    fields=["data.win.eventdata.targetObject", "syscheck.path"],
                    value=stix_entity["key"],
                )
            case "Windows-Registry-Value-Type":
                hash = None
                match stix_entity["data_type"]:
                    case "REG_SZ" | "REG_EXPAND_SZ":
                        hash = sha256(stix_entity["data"].encode("utf-8")).hexdigest()
                    case "REG_BINARY":
                        # The STIX standard says that binary data can be in any form, but in order to be able to use this type of observable at all, support only hex strings:
                        try:
                            hash = sha256(
                                bytes.fromhex(stix_entity["data"])
                            ).hexdigest()
                        except ValueError:
                            self.helper.connector_logger.warning(
                                f"Windows-Registry-Value-Type binary string could not be parsed as a hex string: {stix_entity['data']}"
                            )
                    case _:
                        self.helper.connector_logger.info(
                            f"Windos-Registry-Value-Type of type {stix_entity['data_type']} is not supported"
                        )
                        return None

                return (
                    self.client.search_multi(
                        fields=["syscheck.sha256_after"], value=hash
                    )
                    if hash
                    else None
                )
            # TODO: use wazuh API?:
            case "Process":
                # search data.win.eventdata.(image,sourceImage,targetImage) (and check decoders again for more fields)
                if "command_line" in stix_entity:
                    tokens = re.findall(
                        r"""("[^"]*"|'[^']*'|\S+)""", stix_entity["command_line"]
                    )
                    if len(tokens) < 1:
                        return None

                    self.helper.log_debug(tokens)
                    command = basename(tokens[0])
                    args = [
                        re.sub(r"""^['"]|['"]$""", "", arg).replace("\\", "\\\\\\\\")
                        for arg in tokens[1:]
                    ]
                    # Todo: wildcard: case_insensitive: true
                    arg_queries = [
                        {"wildcard": {"data.win.eventdata.commandLine": f"*{arg}*"}}
                        for arg in args
                    ]
                    return self.client.search(
                        must=[
                            {
                                "wildcard": {
                                    "data.win.eventdata.commandLine": f"*{command}*"
                                }
                            }
                        ]
                        + arg_queries
                    )
                else:
                    return None
            # case "Software":
            # dpkg: package, version, arch
            case "Vulnerability":
                return self.client.search_match(
                    {
                        "data.vulnerability.cve": stix_entity["name"],
                        "data.vulnerability.status": "Active",
                    }
                )
            case "User-Account":
                # search user_id too, perhaps also display_name(?)
                # user_id search in SIDs, like data.win.eventdata.{targetSid,subjectUserSid}
                # user_id: audit.{auid,uid,euid,suid,fsuid}, pam.{uid,euid}
                return self.client.search_multi(
                    fields=[
                        "*.dstuser",
                        "*.srcuser",
                        "*.user",
                        "*.userName",
                        "*.username",
                        "*.source_user",
                        "*.sourceUser",
                        "*.destination_user",
                        "*.LoggedUser",
                        "*.parentUser",
                        "data.win.eventdata.samAccountname",
                        "data.gcp.protoPayload.authenticationInfo.principalEmail",
                        "data.gcp.resource.labels.email_id",
                        "data.office365.UserId",
                    ],
                    value=stix_entity["account_login"],
                )
            case _:
                raise ValueError(
                    f'{entity["entity_type"]} is not a supported entity type'
                )

    def create_agent_stix(self, alert):
        s = alert["_source"]
        id = s["agent"]["id"]
        name = s["agent"]["name"]
        return stix2.Identity(
            # id=Identity.generate_id(name, "system"),
            id=Identity.generate_id(id, "system"),
            **self.stix_common_attrs,
            name=name,
            identity_class="system",
            description=f"Wazuh agent ID {id}",
        )

    def create_sighting_stix(
        self, *, sighter_id: str, metadata: SightingsCollector.Meta
    ):
        return stix2.Sighting(
            id=StixSightingRelationship.generate_id(
                metadata.observable_id,
                sighter_id,
                metadata.first_seen,
                metadata.last_seen,
            ),
            **self.stix_common_attrs,
            first_seen=metadata.first_seen,
            last_seen=metadata.last_seen,
            count=metadata.count,
            where_sighted_refs=[sighter_id],
            # Use a dummy indicator since this field is required:
            sighting_of_ref=self.DUMMY_INDICATOR_ID,
            custom_properties={"x_opencti_sighting_of_ref": metadata.observable_id},
            external_references=self.create_sighting_ext_refs(metadata=metadata),
        )
        # Add summary note?
        # Add alert as note: per alert, per ID

    def create_sighting_ext_refs(self, *, metadata: SightingsCollector.Meta):
        ext_ref_count = 0
        return [
            self.create_sighting_ext_ref(alert=alert, limit_info=capped_at)
            for alerts in metadata.alerts.values()
            # In addition to limit the total number of external references,
            # also limit them per alert rule (pick the last N alerts to get
            # the latest alerts):
            for i, alert in enumerate(alerts[-self.max_extrefs_per_alert_rule :])
            for capped_at in (
                (i + 1, len(alerts), self.max_extrefs_per_alert_rule)
                if len(alerts) > self.max_extrefs_per_alert_rule
                else None,
            )
            if (ext_ref_count := ext_ref_count + 1) <= self.max_extrefs
        ]

    def create_sighting_ext_ref(
        self, *, alert, limit_info: tuple[int, int, int] | None
    ):
        capped_info = (
            [
                (
                    str("Count"),
                    f"{limit_info[0]} of {limit_info[1]} (limited to {limit_info[2]})",
                )
            ]
            if limit_info
            else []
        )
        return stix2.ExternalReference(
            source_name="Wazuh alert",
            description=alert_md_table(alert, capped_info),
            url=urljoin(
                self.app_url,  # type: ignore
                f'app/discover#/context/wazuh-alerts-*/{alert["_id"]}?_a=(columns:!(_source),filters:!())',
            ),
        )

    def create_sighting_alert_notes(
        self, *, sighting_id: str, metadata: SightingsCollector.Meta
    ):
        note_count = 0
        return [
            self.create_note_stix(
                sighting_id=sighting_id, alert=alert, limit_info=capped_at
            )
            for alerts in metadata.alerts.values()
            # In addition to limit the total number of external references,
            # also limit them per alert rule (pick the last N alerts to get
            # the latest alerts):
            for i, alert in enumerate(alerts[-self.max_notes_per_alert_rule :])
            for capped_at in (
                (i + 1, len(alerts), self.max_notes_per_alert_rule)
                if len(alerts) > self.max_notes_per_alert_rule
                else None,
            )
            if (note_count := note_count + 1) <= self.max_notes
        ]

    def create_note_stix(
        self, *, sighting_id, alert, limit_info: tuple[int, int, int] | None
    ):
        s = alert["_source"]
        sighted_at = s["@timestamp"]
        alert_json = json.dumps(s, indent=2)
        capped_info = (
            [
                (
                    str("Count"),
                    f"{limit_info[0]} of {limit_info[1]} (limited to {limit_info[2]})",
                )
            ]
            if limit_info
            else []
        )
        return stix2.Note(
            id=Note.generate_id(
                created=sighted_at,
                content=alert_json,
            ),
            created=sighted_at,
            **self.stix_common_attrs,
            abstract=f"""Wazuh alert "{s['rule']['description']}" for sighting at {sighted_at}""",
            content=alert_md_table(alert, capped_info)
            + "\n\n"
            + f"```json\n{alert_json}\n",
            object_refs=sighting_id,
        )

    def create_summary_note(
        self,
        *,
        result: dict,
        sightings_meta: SightingsCollector,
        sightings_ids: list[str],
    ):
        run_time = datetime.now()
        run_time_string = run_time.isoformat() + "Z"
        abstract = f"Wazuh enrichment at {run_time_string}"
        hits_returned = len(result["hits"]["hits"])
        total_hits = result["hits"]["total"]["value"]
        # TODO: shards info
        content = (
            "## Wazuh enrichment summary\n"
            "\n\n"
            "|Key|Value|\n"
            "|---|---|\n"
            f"|Time|{run_time_string}|\n"
            f"|Hits returned|{hits_returned}|\n"
            f"|Total hits|{total_hits}|\n"
            f"|Max hits|{self.hits_limit}|\n"
            f"|**Dropped**|**{total_hits - hits_returned}**|\n"
            f"|Search since|{self.search_after.isoformat() + 'Z' if self.search_after else '–'}|\n"
            f"|Include filter|{json.dumps(self.search_include) if self.client.include_match else ''}|\n"
            f"|Exclude filter|{json.dumps(self.search_exclude) if self.client.exclude_match else ''}|\n"
            f"|Connector v.|{self.CONNECTOR_VERSION}|\n"
            "\n"
            "### Alerts overview\n"
            "\n"
            "|Rule|Level|Count|Earliest|Latest|Description|\n"
            "|----|-----|-----|--------|------|-----------|\n"
        ) + "".join(
            f"{rule_id}|{level}|{len(alerts)}{'+' if total_hits > hits_returned else ''}|{sightings_meta.first_seen(rule_id)}|{sightings_meta.last_seen(rule_id)}|{rule_desc}|\n"
            for rule_id, alerts in sightings_meta.alerts_by_rule_id().items()
            for level in (alerts[0]["_source"]["rule"]["level"],)
            for rule_desc in (
                common_prefix_string(
                    [alert["_source"]["rule"]["description"] for alert in alerts]
                ),
            )
        )

        return stix2.Note(
            id=Note.generate_id(created=run_time_string, content=content),
            created=run_time_string,
            **self.stix_common_attrs,
            abstract=abstract,
            content=content,
            object_refs=[sightings_meta.observable_id()] + sightings_ids,
        )

    def create_incidents(
        self,
        *,
        entity: dict,
        obs_indicators: list[dict],
        result: dict,
        sightings_meta: SightingsCollector,
    ):
        sightings = sightings_meta.collated()
        incidents = []
        bundle = []
        total_sightings = sum(map(lambda s: s.count, sightings.values()))
        total_systems = len(sightings.keys())
        query_hits_dropped = (
            len(result["hits"]["hits"]) < result["hits"]["total"]["value"]
        )
        # TODO:
        # severity = cvss3_to_severity(alert entity['entity_type'] == 'Vulnerability'
        match self.create_incident:
            case "per_query":
                incident_name = f"Wazuh alert: {entity_name_value(entity)} sighted"
                incident = stix2.Incident(
                    id=Incident.generate_id(
                        incident_name, sightings_meta.last_sighting_timestamp()
                    ),
                    created=sightings_meta.last_sighting_timestamp(),
                    **self.stix_common_attrs,
                    incident_type="alert",
                    name=incident_name,
                    description=f"Observable {entity_name_value(entity)} has been sighted a total of {total_sightings}{'+' if query_hits_dropped else ''} time(s) in {total_systems} system(s)",
                    allow_custom=True,
                    # The following are extensions:
                    severity=rule_level_to_severity(sightings_meta.max_rule_level()),
                    first_seen=sightings_meta.first_seen(),
                    last_seen=sightings_meta.last_seen(),
                )
                incidents = [incident]
                bundle = incidents + self.create_incident_relationships(
                    incident=incident,
                    entity=entity,
                    obs_indicators=obs_indicators,
                    sighters=list(sightings.keys()),
                )

            case "per_sighting":
                for sighter_id, meta in sightings.items():
                    incident_name = f"Wazuh alert: {entity_name_value(entity)} sighted in {meta.sighter_name}"
                    incident = stix2.Incident(
                        id=Incident.generate_id(incident_name, meta.last_seen),
                        created=meta.last_seen,
                        **self.stix_common_attrs,
                        incident_type="alert",
                        name=incident_name,
                        description=f"Observable {entity_name_value(entity)} has been sighted {meta.count}{'+' if query_hits_dropped else ''} time(s) in {meta.sighter_name}",
                        allow_custom=True,
                        # The following are extensions:
                        severity=rule_level_to_severity(meta.max_rule_level),
                        first_seen=meta.first_seen,
                        last_seen=meta.last_seen,
                    )
                    incidents.append(incident)
                    bundle.append(incident)
                    bundle += self.create_incident_relationships(
                        incident=incident,
                        entity=entity,
                        obs_indicators=obs_indicators,
                        sighters=[sighter_id],
                    )

            case "per_alert_rule":
                for rule_id, meta in sightings_meta.alerts_by_rule_meta().items():
                    incident_name = f"Wazuh alert: {entity_name_value(entity)} sighted"
                    # Alerts are grouped by ID and all have the same level, so just pick one:
                    alerts_level = meta["alerts"][0]["_source"]["rule"]["level"]
                    # Alerts may have different description even if they have
                    # the same level. Pick the longest common prefix:
                    alerts_desc = common_prefix_string(
                        [
                            alert["_source"]["rule"]["description"]
                            for alert in meta["alerts"]
                        ]
                    )
                    incident = stix2.Incident(
                        id=Incident.generate_id(incident_name, meta["last_seen"]),
                        created=meta["last_seen"],
                        **self.stix_common_attrs,
                        incident_type="alert",
                        name=incident_name,
                        description=f"""Observable {entity_name_value(entity)} has been sighted {len(meta['alerts'])}{'+' if query_hits_dropped else ''} time(s) in alert rule {rule_id}: "{alerts_desc}\"""",
                        allow_custom=True,
                        # The following are extensions:
                        severity=rule_level_to_severity(alerts_level),
                        first_seen=meta["first_seen"],
                        last_seen=meta["last_seen"],
                    )
                    incidents.append(incident)
                    bundle.append(incident)
                    bundle += self.create_incident_relationships(
                        incident=incident,
                        entity=entity,
                        obs_indicators=obs_indicators,
                        sighters=meta["sighters"],
                    )

            case "per_alert":
                for sighter_id, meta in sightings_meta.alerts_by_sighter_meta().items():
                    incident_name = f"Wazuh alert: {entity_name_value(entity)} sighted in {meta['sighter_name']}"
                    incidents = [
                        stix2.Incident(
                            id=Incident.generate_id(incident_name, sighted_at),
                            created=sighted_at,
                            **self.stix_common_attrs,
                            incident_type="alert",
                            name=incident_name,
                            description=f"""Observable {entity_name_value(entity)} has been sighted in alert rule {rule_id}: "{rule_desc}\"""",
                            allow_custom=True,
                            # The following are extensions:
                            severity=rule_level_to_severity(rule_level),
                            first_seen=sighted_at,
                            last_seen=sighted_at,
                        )
                        for alert in meta["alerts"]
                        for sighted_at in (alert["_source"]["@timestamp"],)
                        for rule_id in (alert["_source"]["rule"]["id"],)
                        for rule_desc in (alert["_source"]["rule"]["description"],)
                        for rule_level in (alert["_source"]["rule"]["level"],)
                    ]
                    bundle += incidents

                    for incident in incidents:
                        bundle += self.create_incident_relationships(
                            incident=incident,
                            entity=entity,
                            obs_indicators=obs_indicators,
                            sighters=[sighter_id],
                        )

            case _:
                raise ValueError(
                    f'WAZUH_INCIDENT_CREATE_MODE "{self.create_incident}" is invalid'
                )

        bundle += [
            obj
            for incident in incidents
            for obj in self.enrich_incident(
                incident=incident, alerts=sightings_meta.alerts()
            )
        ]
        # TODO: add note
        return bundle

    def create_incident_relationships(
        self,
        *,
        incident: stix2.Incident,
        entity: dict,
        obs_indicators: list[dict],
        sighters: list[str],
    ):
        return (
            [
                stix2.Relationship(
                    id=StixCoreRelationship.generate_id(
                        incident_entity_relation_type(entity),
                        incident.id,
                        entity["standard_id"],
                    ),
                    created=incident.created,
                    **self.stix_common_attrs,
                    relationship_type=incident_entity_relation_type(entity),
                    source_ref=incident.id,
                    target_ref=entity["standard_id"],
                )
            ]
            + [
                stix2.Relationship(
                    id=StixCoreRelationship.generate_id(
                        "targets",
                        incident.id,
                        sighter,
                    ),
                    created=incident.created,
                    **self.stix_common_attrs,
                    relationship_type="targets",
                    source_ref=incident.id,
                    target_ref=sighter,
                )
                for sighter in sighters
            ]
            + [
                stix2.Relationship(
                    id=StixCoreRelationship.generate_id(
                        "indicates",
                        ind["standard_id"],
                        incident.id,
                    ),
                    created=incident.created,
                    **self.stix_common_attrs,
                    relationship_type="indicates",
                    source_ref=ind["standard_id"],
                    target_ref=incident.id,
                )
                for ind in obs_indicators
            ]
        )

    def enrich_incident(self, *, incident: stix2.Incident, alerts: list[dict]):
        # TODO: enable based on a list (…ENRICH=mitre,tool,account)
        return (
            self.enrich_incident_mitre(incident=incident, alerts=alerts)
            + self.enrich_incident_tool(incident=incident, alerts=alerts)
            ## Perhaps not very useful:
            # + self.enrich_accounts(incident=incident, alerts=alerts)
        )

    def enrich_incident_mitre(self, *, incident: stix2.Incident, alerts: list[dict]):
        bundle = []
        mitre_ids = [
            id
            for alert in alerts[0:1]
            for rule in (alert["_source"]["rule"],)
            if "mitre" in rule
            for id in rule["mitre"]["id"]
        ]
        self.helper.log_debug(f"MITRE IDS: {mitre_ids}")
        for mitre_id in mitre_ids:
            pattern = stix2.AttackPattern(
                id=AttackPattern.generate_id(mitre_id, mitre_id),
                name=mitre_id,
                allow_custom=True,
                x_mitre_id=mitre_id,
            )
            bundle += [
                pattern,
                stix2.Relationship(
                    id=StixCoreRelationship.generate_id(
                        "uses", incident.id, pattern.id
                    ),
                    created=alerts[0]["_source"]["@timestamp"],
                    **self.stix_common_attrs,
                    relationship_type="uses",
                    source_ref=incident.id,
                    target_ref=pattern.id,
                ),
            ]

        return bundle

    def enrich_incident_tool(self, *, incident: stix2.Incident, alerts: list[dict]):
        bundle = []
        for alert in alerts:
            tool = None
            if (
                has(alert, ["_source", "rule", "mitre", "id"])
                and "T1053.005" in alert["_source"]["rule"]["mitre"]["id"]
            ):
                tool = create_tool_stix("schtasks")
            elif "psexec" in alert["_source"]["rule"]["description"].casefold():
                tool = create_tool_stix("PsExec")

            if tool:
                bundle += [
                    tool,
                    stix2.Relationship(
                        id=StixCoreRelationship.generate_id(
                            "uses", incident.id, tool.id
                        ),
                        created=alert["_source"]["@timestamp"],
                        **self.stix_common_attrs,
                        relationship_type="uses",
                        source_ref=incident.id,
                        target_ref=tool.id,
                    ),
                ]

        return bundle

    def enrich_accounts(self, *, incident: stix2.Incident, alerts: list[dict]):
        bundle = []
        for alert in alerts:
            # Remove invalid usernames (like 'root(uid=0)', or just remove any (uid=…) part.
            if has_any(alert, ["_source", "data"], ["dstuser", "srcuser"]):
                accounts = [
                    stix2.UserAccount(
                        account_login=username,
                        allow_custom=True,
                        **self.stix_common_attrs,
                    )
                    for data in (alert["_source"]["data"],)
                    for field, username in data.items()
                    if field in {"dstuser", "srcuser"}
                ]
                bundle += accounts + [
                    stix2.Relationship(
                        id=StixCoreRelationship.generate_id(
                            "related-to", incident.id, account.id
                        ),
                        created=alert["_source"]["@timestamp"],
                        **self.stix_common_attrs,
                        relationship_type="related-to",
                        source_ref=incident.id,
                        target_ref=account.id,
                    )
                    for account in accounts
                ]

        return bundle

    def create_agent_addr_obs(self, *, alerts: list[dict]):
        agents = {
            agent["id"]: {
                "name": agent["name"],
                "ip": ipaddress.ip_address(agent["ip"]),
                "standard_id": Identity.generate_id(agent["id"], "system"),
            }
            for alert in alerts
            for agent in (alert["_source"]["agent"],)
        }
        bundle = []
        for agent in agents.values():
            SCO = (
                stix2.IPv4Address
                if type(agent["ip"]) is ipaddress.IPv4Address
                else stix2.IPv6Address
            )
            addr = SCO(
                value=agent["ip"],
                allow_custom=True,
                **self.stix_common_attrs,
            )
            rel = stix2.Relationship(
                id=StixCoreRelationship.generate_id(
                    "related-to", agent["standard_id"], addr.id
                ),
                **self.stix_common_attrs,
                relationship_type="related-to",
                source_ref=agent["standard_id"],
                target_ref=addr.id,
            )
            bundle.append(addr)
            bundle.append(rel)

        return bundle

    def create_agent_hostname_obs(self, *, alerts: list[dict]):
        agents = {
            agent["id"]: {
                "name": agent["name"],
                "standard_id": Identity.generate_id(agent["id"], "system"),
            }
            for alert in alerts
            for agent in (alert["_source"]["agent"],)
        }
        bundle = []
        for agent in agents.values():
            hostname = CustomObservableHostname(
                value=agent["name"],
                allow_custom=True,
                **self.stix_common_attrs,
            )
            rel = stix2.Relationship(
                id=StixCoreRelationship.generate_id(
                    "related-to", agent["standard_id"], hostname.id
                ),
                **self.stix_common_attrs,
                relationship_type="related-to",
                source_ref=agent["standard_id"],
                target_ref=hostname.id,
            )
            bundle.append(hostname)
            bundle.append(rel)

        return bundle
