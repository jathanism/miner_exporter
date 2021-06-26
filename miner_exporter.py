#!/usr/bin/env python3

# internal packages
import datetime
import logging
import os
import re
import time

# external packages
import dateutil.parser
import docker
import prometheus_client
import psutil
import requests

# Remember, levels: debug, info, warning, error, critical. there is no trace.
logging.basicConfig(
    format="%(filename)s:%(funcName)s:%(lineno)d:%(levelname)s\t%(message)s",
    level=logging.WARNING,
)
log = logging.getLogger(__name__)
log.setLevel(logging.INFO)
# log.setLevel(logging.DEBUG)

# Time to sleep between scrapes
UPDATE_PERIOD = int(os.environ.get("UPDATE_PERIOD", 30))
VALIDATOR_CONTAINER_NAME = os.environ.get("VALIDATOR_CONTAINER_NAME", "validator")

# Helium API URL (mainnet)
# For testnet: https://testnet-api.helium.wtf/v1
API_BASE_URL = os.environ.get("API_BASE_URL", "https://api.helium.io/v1")

# Use the RPC calls where available. This means you have your RPC port open.
# Once all of the exec calls are replaced we can enable this by default.
# FIXME(jathanism): This doesn't seem to do anything
ENABLE_RPC = os.environ.get("ENABLE_RPC", 0)

# Prometheus exporter types Gauge,Counter,Summary,Histogram,Info and Enum
SCRAPE_TIME = prometheus_client.Summary("validator_scrape_time", "Time spent collecting miner data")
SYSTEM_USAGE = prometheus_client.Gauge(
    "system_usage",
    "Hold current system resource usage",
    ["resource_type", "validator_name"],
)
CHAIN_STATS = prometheus_client.Gauge(
    "chain_stats", "Stats about the global chain", ["resource_type"]
)
VAL = prometheus_client.Gauge(
    "validator_height",
    "Height of the validator's blockchain",
    ["resource_type", "validator_name"],
)
INCON = prometheus_client.Gauge(
    "validator_inconsensus",
    "Is validator currently in consensus group",
    ["validator_name"],
)
BLOCKAGE = prometheus_client.Gauge(
    "validator_block_age",
    "Age of the current block",
    ["resource_type", "validator_name"],
)
HBBFT_PERF = prometheus_client.Gauge(
    "validator_hbbft_perf",
    "HBBFT performance metrics from perf, only applies when in CG",
    ["resource_type", "subtype", "validator_name"],
)
CONNECTIONS = prometheus_client.Gauge(
    "validator_connections",
    "Number of libp2p connections ",
    ["resource_type", "validator_name"],
)
SESSIONS = prometheus_client.Gauge(
    "validator_sessions",
    "Number of libp2p sessions",
    ["resource_type", "validator_name"],
)
LEDGER_PENALTY = prometheus_client.Gauge(
    "validator_ledger",
    "Validator performance metrics ",
    ["resource_type", "subtype", "validator_name"],
)
VALIDATOR_VERSION = prometheus_client.Info(
    "validator_version", "Version number of the miner container", ["validator_name"]
)
BALANCE = prometheus_client.Gauge(
    "validator_api_balance",
    "Balance of the validator owner account",
    ["validator_name"],
)
UPTIME = prometheus_client.Gauge(
    "validator_container_uptime",
    "Time container has been at a given state",
    ["state_type", "validator_name"],
)


def try_int(v):
    if re.match(r"^\-?\d+$", v):
        return int(v)
    return v


def try_float(v):
    if re.match(r"^\-?[\d\.]+$", v):
        return float(v)
    return v


class MinerExporter:
    def __init__(
        self,
        container_name=VALIDATOR_CONTAINER_NAME,
        api_base_url=API_BASE_URL,
        update_period=UPDATE_PERIOD,
        enable_rpc=ENABLE_RPC,
    ):
        self.container_name = container_name
        self.api_base_url = api_base_url
        self.update_period = update_period
        self.enable_rpc = enable_rpc

        # Get the Docker container object
        self.docker = self.get_docker_container(self.container_name)

        # Store the name
        facts = self.get_facts()
        self.miner_name = facts["name"]
        self.address = facts["address"]

    def __repr__(self):
        return f"<MinerExporter: {self.container_name}>"

    def get_docker_container(self, container_name=None):
        if container_name is None:
            container_name = self.container_name

        try:
            dc = docker.DockerClient()

            # Try to find by specific name first
            docker_container = dc.containers.get(container_name)
        except docker.errors.NotFound as ex:
            # If find by specifc name fails, try to find by prefix
            containers = dc.containers.list()

            for container in containers:
                if container.name.startswith(container_name):
                    docker_container = container
                    break

            # If container not found, then log error and return
            if docker_container is None:
                log.error(f"docker failed while bootstrapping. Not exporting anything. Error: {ex}")
                return

        return docker_container

    def exec_run(self, command):
        out = self.docker.exec_run(command)
        log.debug(out.output)
        return out.output.decode("utf-8").rstrip("\n")

    #
    # Getters
    #

    def get_facts(self):

        # miner_facts = {
        #  'name': None,
        #  'address': None
        # }
        # FIXME(jathan): This just looks like something here for debug?
        """
        if miner_facts:
            return miner_facts
        """

        # sample output:
        # {pubkey,"1YBkf..."}.
        # {onboarding_key,"1YBkf..."}.
        # {animal_name,"one-two-three"}.
        out = self.exec_run("miner print_keys")

        miner_facts = {}
        print_keys = {}
        for line in out.splitlines():

            # := requires py3.8
            if m := re.match(r'{([^,]+),"([^"]+)"}.', line):
                log.debug(m)
                k = m.group(1)
                v = m.group(2)
                log.debug(k, v)
                print_keys[k] = v

        if v := print_keys.get("pubkey"):
            miner_facts["address"] = v
        if print_keys.get("animal_name"):
            miner_facts["name"] = v

        # $ docker exec validator miner print_keys
        return miner_facts

    def get_miner_name(self):
        # TODO: need to fix this. hotspot name really should only be queried once
        hotspot_name = self.exec_run("miner info name")
        return hotspot_name

    #
    # Collectors
    #

    def collect_miner_height(self):
        # grab the local blockchain height (it returns a 2-tuple)
        out = self.exec_run("miner info height")
        VAL.labels("Height", self.miner_name).set(out.split()[1])

    def collect_container_run_time(self):
        attrs = self.docker.attrs

        # examples and other things we could track:
        # "Created": "2021-05-18T22:11:48.962678927Z",
        # "Id": "cd611b83a0f267a1000603db52aa2d21247a32cc195c9c2b8ebcade5d35cfe1a",
        # "State": {
        #   "Status": "running",
        #   "Running": true,
        #   "Paused": false,
        #   "Restarting": false,
        #   "OOMKilled": false,
        #   "Dead": false,
        #   "Pid": 4159823,
        #   "ExitCode": 0,
        #   "Error": "",
        #   "StartedAt": "2021-05-18T22:11:49.50436001Z",
        #   "FinishedAt": "0001-01-01T00:00:00Z"

        now = datetime.datetime.now(datetime.timezone.utc)

        if attrs:
            if attrs.get("Created"):
                create_time = attrs.get("Created")
                create_dt = dateutil.parser.parse(create_time)
                create_delta = (now - create_dt).total_seconds()
                UPTIME.labels("create", self.miner_name).set(create_delta)
            if attrs.get("State") and attrs["State"].get("StartedAt"):
                start_time = attrs["State"]["StartedAt"]
                start_dt = dateutil.parser.parse(start_time)
                start_delta = (now - start_dt).total_seconds()
                UPTIME.labels("start", self.miner_name).set(start_delta)

    def collect_chain_stats(self):
        height = safe_get_json(f"{self.api_base_url}/blocks/height")
        if not height:
            log.error("chain height fetch returned empty JSON")
            return

        height_val = height["data"]["height"]
        CHAIN_STATS.labels("height").set(height_val)

        stats = safe_get_json(f"{self.api_base_url}/validators/stats")
        if not stats:
            log.error("val stats stats fetch returned empty JSON")
            return

        count_val = stats["data"]["staked"]["count"]
        CHAIN_STATS.labels("staked_validators").set(count_val)

    def collect_balance(self):
        api_validators = safe_get_json(f"{API_BASE_URL}/validators/{self.address}")

        if not api_validators:
            log.error("validator fetch returned empty JSON")
            return
        elif not api_validators.get("data") or not api_validators["data"].get("owner"):
            log.error("could not find validator data owner in json")
            return

        # Owner address
        owner_address = api_validators["data"]["owner"]

        api_accounts = safe_get_json(f"{API_BASE_URL}/accounts/{owner_address}")
        if not api_accounts:
            return
        if not api_accounts.get("data") or not api_accounts["data"].get("balance"):
            return
        balance = float(api_accounts["data"]["balance"]) / 1e8

        # print(api_accounts)
        # print('balance',balance)
        BALANCE.labels(self.miner_name).set(balance)

    def collect_in_consensus(self):
        # check if currently in consensus group
        incon_txt = self.exec_run("miner info in_consensus")

        incon = 0
        if incon_txt == "true":
            incon = 1

        log.info(f"in consensus? {incon} / {incon_txt}")
        INCON.labels(self.miner_name).set(incon)

    def collect_block_age(self):
        # collect current block age & cast to int
        age_val = try_int(self.exec_run("miner info block_age"))
        BLOCKAGE.labels("BlockAge", self.miner_name).set(age_val)
        log.debug(f"age: {age_val}")

    def collect_miner_version(self):
        out = self.exec_run("miner versions")
        results = out.splitlines()

        # sample output
        # $ docker exec validator miner versions
        # Installed versions:
        # * 0.1.48	permanent
        for line in results:
            if m := re.match(r"^\*\s+([\d\.]+)(.*)", line):
                miner_version = m.group(1)
                log.info(f"found miner version: {miner_version}")
                VALIDATOR_VERSION.labels(self.miner_name).info({"version": miner_version})

    def collect_ledger_validators(self):
        # ledger validators output
        out = self.exec_run("miner ledger validators --format csv")
        results = out.splitlines()

        # parse the ledger validators output
        for line in [x.rstrip("\r\n") for x in results]:
            c = line.split(",")
            # print(f"{len(c)} {c}")
            if len(c) == 10:
                if c[0] == "name" and c[1] == "owner_address":
                    # header line
                    continue

                (
                    val_name,
                    address,
                    last_heartbeat,
                    stake,
                    status,
                    version,
                    tenure_penalty,
                    dkg_penalty,
                    performance_penalty,
                    total_penalty,
                ) = c
                if self.miner_name == val_name:
                    log.debug(f"have pen line: {c}")
                    tenure_penalty_val = try_float(tenure_penalty)
                    dkg_penalty_val = try_float(dkg_penalty)
                    performance_penalty_val = try_float(performance_penalty)
                    total_penalty_val = try_float(total_penalty)
                    # last_heartbeat = try_float(last_heartbeat)

                    log.info(f"L penalty: {total_penalty_val}")
                    LEDGER_PENALTY.labels("ledger_penalties", "tenure", self.miner_name).set(
                        tenure_penalty_val
                    )
                    LEDGER_PENALTY.labels("ledger_penalties", "dkg", self.miner_name).set(
                        dkg_penalty_val
                    )
                    LEDGER_PENALTY.labels("ledger_penalties", "performance", self.miner_name).set(
                        performance_penalty_val
                    )
                    LEDGER_PENALTY.labels("ledger_penalties", "total", self.miner_name).set(
                        total_penalty_val
                    )
                    BLOCKAGE.labels("last_heartbeat", self.miner_name).set(last_heartbeat)

            elif len(line) == 0:
                # empty lines are fine
                pass
            else:
                log.warning(f"failed to grok line: {c}; section count: {len(c)}")

    def collect_peer_book(self):
        """Parse the peer book output."""
        # peer book -s output
        out = self.exec_run("miner peer book -s --format csv")

        # samples
        # address,name,listen_addrs,connections,nat,last_updated
        # /p2p/1YBkfTYH8iCvchuTevbCAbdni54geDjH95yopRRznZtAur3iPrM,bright-fuchsia-sidewinder,1,6,none,203.072s
        # listen_addrs (prioritized)
        # /ip4/174.140.164.130/tcp/2154
        # local,remote,p2p,name
        # /ip4/192.168.0.4/tcp/2154,/ip4/72.224.176.69/tcp/2154,/p2p/1YU2cE9FNrwkTr8RjSBT7KLvxwPF9i6mAx8GoaHB9G3tou37jCM,clever-sepia-bull

        # TODO(jathanism): Replace this parsing w/ csv.reader
        sessions = 0
        for line in out.splitlines():
            c = line.split(",")
            if len(c) == 6:
                log.debug(f"peerbook entry6: {c}")
                (address, peer_name, listen_add, connections, nat, last_update) = c
                conns_num = try_int(connections)

                if self.miner_name == peer_name and isinstance(conns_num, int):
                    CONNECTIONS.labels("connections", self.miner_name).set(conns_num)

            elif len(c) == 4:
                # local,remote,p2p,name
                log.debug(f"peerbook entry4: {c}")
                if c[0] != "local":
                    sessions += 1
            elif len(c) == 1:
                log.debug(f"peerbook entry1: {c}")
                # listen_addrs
                pass
            else:
                log.warning(f"could not understand peer book line: {c}")

        log.debug(f"sess: {sessions}")
        SESSIONS.labels("sessions", self.miner_name).set(sessions)

    # persist these between calls
    # hval = {}

    def collect_hbbft_performance(self, hval={}):
        """Parse the hbbft performance table for the penalty field."""
        out = self.exec_run("miner hbbft perf --format csv")

        # TODO(jathanism): Replace this parsing w/ csv.reader
        for line in out.splitlines():
            c = [x.strip() for x in line.split(",")]
            # samples:

            # FIXME(jathan): This is unused. Remove?
            # have_data = False

            if len(c) == 7 and self.miner_name == c[0]:
                # name,bba_completions,seen_votes,last_bba,last_seen,tenure,penalty
                # great-clear-chinchilla,5/5,237/237,0,0,2.91,2.91
                log.debug(f"resl7: {c}; {self.miner_name}/{c[0]}")

                (hval["bba_votes"], hval["bba_tot"]) = c[1].split("/")
                (hval["seen_votes"], hval["seen_tot"]) = c[2].split("/")
                hval["bba_last_val"] = try_float(c[3])
                hval["seen_last_val"] = try_float(c[4])
                hval["tenure"] = try_float(c[5])
                hval["pen_val"] = try_float(c[6])
            elif len(c) == 6 and self.miner_name == c[0]:
                # name,bba_completions,seen_votes,last_bba,last_seen,penalty
                # curly-peach-owl,11/11,368/368,0,0,1.86
                log.debug(f"resl6: {c}; {self.miner_name}/{c[0]}")

                (hval["bba_votes"], hval["bba_tot"]) = c[1].split("/")
                (hval["seen_votes"], hval["seen_tot"]) = c[2].split("/")
                hval["bba_last_val"] = try_float(c[3])
                hval["seen_last_val"] = try_float(c[4])
                hval["pen_val"] = try_float(c[5])

            elif len(c) == 6:
                # Not our line
                pass
            elif len(line) == 0:
                # Empty line
                pass
            else:
                log.debug(f"wrong len ({len(c)}) for hbbft: {c}")

            # Always set these, that way they get reset when out of CG
            HBBFT_PERF.labels("hbbft_perf", "Penalty", self.miner_name).set(hval.get("pen_val", 0))
            HBBFT_PERF.labels("hbbft_perf", "BBA_Total", self.miner_name).set(
                hval.get("bba_tot", 0)
            )
            HBBFT_PERF.labels("hbbft_perf", "BBA_Votes", self.miner_name).set(
                hval.get("bba_votes", 0)
            )
            HBBFT_PERF.labels("hbbft_perf", "Seen_Total", self.miner_name).set(
                hval.get("seen_tot", 0)
            )
            HBBFT_PERF.labels("hbbft_perf", "Seen_Votes", self.miner_name).set(
                hval.get("seen_votes", 0)
            )
            HBBFT_PERF.labels("hbbft_perf", "BBA_Last", self.miner_name).set(
                hval.get("bba_last_val", 0)
            )
            HBBFT_PERF.labels("hbbft_perf", "Seen_Last", self.miner_name).set(
                hval.get("seen_last_val", 0)
            )
            HBBFT_PERF.labels("hbbft_perf", "Tenure", self.miner_name).set(hval.get("tenure", 0))


# Decorate function with metric.
@SCRAPE_TIME.time()
def stats():

    exporter = MinerExporter()
    miner_name = exporter.miner_name

    # Collect total cpu and memory usage. Might want to consider just the
    # Docker container with something like cadvisor instead.
    SYSTEM_USAGE.labels("CPU", miner_name).set(psutil.cpu_percent())
    SYSTEM_USAGE.labels("Memory", miner_name).set(psutil.virtual_memory()[2])

    # Collect all the stats from the miner.
    exporter.collect_container_run_time()
    exporter.collect_miner_version()
    exporter.collect_block_age()
    exporter.collect_miner_height()
    exporter.collect_chain_stats()
    exporter.collect_in_consensus()
    exporter.collect_ledger_validators()
    exporter.collect_peer_book()
    exporter.collect_hbbft_performance()
    exporter.collect_balance()


def safe_get_json(url):
    try:
        ret = requests.get(url)
        if not ret.status_code == requests.codes.ok:
            log.error(f"bad status code ({ret.status_code}) from url: {url}")
            return
        retj = ret.json()
        return retj

    except (requests.exceptions.SSLError, requests.exceptions.ConnectionError) as ex:
        log.error(f"error fetching {url}: {ex}")
        return


if __name__ == "__main__":
    prometheus_client.start_http_server(9825)  # 9-VAL on your phone
    while True:
        # log.warning("starting loop.")
        try:
            stats()
        except ValueError as ex:
            log.error("stats loop failed.", exc_info=ex)
        except docker.errors.APIError as ex:
            log.error("stats loop failed with a docker error.", exc_info=ex)

        # Sleep 30 seconds
        time.sleep(UPDATE_PERIOD)
