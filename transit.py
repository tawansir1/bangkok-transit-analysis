"""
transit.py — load the Bangkok rail and boat network and price any trip.

The same logic as the route planner on the Bangkok Guide website,
rewritten in Python so the whole network can be analysed at once.

    from transit import Network
    net = Network.load("data")
    trip = net.trip("E4", "BL29")      # Asok -> Wat Mangkon
    print(trip.fare, trip.minutes)
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from pathlib import Path

import networkx as nx
import pandas as pd

WAIT_TO_BOARD = 4      # minutes on the platform before a ride
BTS_FARE_CAP = 65      # one BTS trip never costs more than this
ROAD_FACTOR = 1.2      # roads are about 20% longer than the rail line
RUSH_MIN_PER_KM = 1.5  # extra minutes stuck in traffic per km at rush hour


@dataclass
class Leg:
    line: str
    stops: list[str]
    minutes: float

    @property
    def hops(self) -> int:
        return len(self.stops) - 1


@dataclass
class Trip:
    origin: str
    destination: str
    minutes: float
    legs: list[Leg]
    rides: list[dict] = field(default_factory=list)

    @property
    def fare(self) -> int:
        return sum(r["fare"] for r in self.rides)

    @property
    def operators(self) -> int:
        """How many separate tickets you need."""
        return len(self.rides)

    @property
    def ride_stops(self) -> int:
        return sum(l.hops for l in self.legs if l.line != "walk")


class Network:
    def __init__(self, stations, links, lines, fares, taxi):
        self.stations = stations
        self.links = links
        self.lines = lines.set_index("line")
        self.fares = fares
        self.taxi = taxi
        self.name = stations.drop_duplicates("code").set_index("code")["name"].to_dict()
        self.bts_core = set(stations.loc[stations.bts_core == 1, "code"])
        self.system = self.lines["system"].to_dict()

        # adjacency list: node -> [(neighbour, line, minutes)]
        self.adj: dict[str, list[tuple[str, str, float]]] = {}
        for r in links.itertuples():
            self.adj.setdefault(r._1, []).append((r.to, r.line, r.minutes))
            self.adj.setdefault(r.to, []).append((r._1, r.line, r.minutes))

    # ---------- loading ----------
    @classmethod
    def load(cls, folder: str | Path = "data") -> "Network":
        folder = Path(folder)
        return cls(
            stations=pd.read_csv(folder / "stations.csv"),
            links=pd.read_csv(folder / "links.csv"),
            lines=pd.read_csv(folder / "lines.csv"),
            fares=pd.read_csv(folder / "fares.csv"),
            taxi=pd.read_csv(folder / "taxi_meter.csv", comment="#"),
        )

    def graph(self) -> nx.Graph:
        """The network as a networkx graph, weighted by minutes."""
        g = nx.Graph()
        for r in self.links.itertuples():
            g.add_edge(r._1, r.to, line=r.line, minutes=r.minutes)
        for code, name in self.name.items():
            if code in g:
                g.nodes[code]["name"] = name
        return g

    # ---------- shortest path (Dijkstra) ----------
    def _search(self, start: str):
        """Dijkstra from one station. A state is (station, line you arrived on),
        so changing line costs the platform wait."""
        best = {(start, None): 0.0}
        prev = {}
        heap = [(0.0, start, None)]
        while heap:
            cost, node, line = heapq.heappop(heap)
            if cost > best[(node, line)]:
                continue
            for nxt, edge_line, minutes in self.adj.get(node, []):
                new = cost + minutes
                riding = edge_line != "walk"
                if riding and (line is None or (line != "walk" and line != edge_line)):
                    new += WAIT_TO_BOARD
                if edge_line == "walk":
                    new += WAIT_TO_BOARD  # walking transfers include the wait
                key = (nxt, edge_line)
                if new < best.get(key, float("inf")):
                    best[key] = new
                    prev[key] = (node, line, edge_line, minutes)
                    heapq.heappush(heap, (new, nxt, edge_line))
        return best, prev

    def _rebuild(self, start, end, best, prev) -> Trip | None:
        ends = [(c, k) for k, c in best.items() if k[0] == end]
        if not ends:
            return None
        cost, key = min(ends)
        edges = []
        while key in prev:
            node, line, edge_line, minutes = prev[key]
            edges.append((node, key[0], edge_line, minutes))
            key = (node, line)
        edges.reverse()

        legs: list[Leg] = []
        for a, b, line, minutes in edges:
            if legs and legs[-1].line == line:
                legs[-1].stops.append(b)
                legs[-1].minutes += minutes
            else:
                legs.append(Leg(line, [a, b], minutes))
        trip = Trip(start, end, cost, legs)
        trip.rides = self._price(legs)
        return trip

    def trip(self, start: str, end: str) -> Trip | None:
        best, prev = self._search(start)
        return self._rebuild(start, end, best, prev)

    # ---------- fares ----------
    def _table(self, name: str, stops: int) -> int:
        t = self.fares[self.fares.table == name]
        stops = min(stops, t.stops.max())
        return int(t.loc[t.stops == stops, "fare_thb"].iloc[0])

    def _price(self, legs: list[Leg]) -> list[dict]:
        """Group legs into rides (one ticket each) and price them.
        Sukhumvit + Silom Line at Siam count as one BTS ride."""
        rides = []
        for i, leg in enumerate(legs):
            system = self.system[leg.line]
            if system == "walk":
                continue
            continues = (rides and rides[-1]["system"] == system == "bts"
                         and i > 0 and legs[i - 1] is rides[-1]["legs"][-1])
            if continues:
                rides[-1]["legs"].append(leg)
            else:
                rides.append({"system": system, "legs": [leg]})

        for r in rides:
            stops = sum(l.hops for l in r["legs"])
            s = r["system"]
            if s == "bts":
                core = ext = 0
                for l in r["legs"]:
                    for a, b in zip(l.stops, l.stops[1:]):
                        if a in self.bts_core and b in self.bts_core:
                            core += 1
                        else:
                            ext += 1
                fare = (self._table("bts_core", core) if core else 0) + \
                       (self._table("bts_extension", ext) if ext else 0)
                r["fare"] = min(BTS_FARE_CAP, fare)
            elif s == "mrt":
                r["fare"] = self._table("mrt_blue", stops)
            else:
                r["fare"] = self._table(s, stops)
            r["stops"] = stops
        return rides

    # ---------- taxi ----------
    def taxi_meter(self, km: float) -> float:
        """Metered fare for a distance, no traffic."""
        fare, done = 35.0, 1.0
        for r in self.taxi.itertuples():
            if km <= done:
                break
            upto = r.to_km if pd.notna(r.to_km) else float("inf")
            part = min(km, upto) - done
            fare += part * r.thb_per_km
            done = min(km, upto)
        return fare

    def road_km(self, trip: Trip) -> float:
        km = sum(l.hops * self.lines.at[l.line, "km_per_stop"] for l in trip.legs)
        return max(1.0, km * ROAD_FACTOR)

    # ---------- every trip at once ----------
    def all_trips(self, nodes: list[str] | None = None) -> pd.DataFrame:
        """Fastest trip between every pair of stations, one row per pair."""
        nodes = nodes or sorted(self.adj)
        rows = []
        for a in nodes:
            best, prev = self._search(a)
            for b in nodes:
                if a == b:
                    continue
                t = self._rebuild(a, b, best, prev)
                if t is None:
                    continue
                km = self.road_km(t)
                taxi = self.taxi_meter(km)
                rows.append({
                    "from": a, "to": b,
                    "minutes": round(t.minutes, 1),
                    "fare": t.fare,
                    "tickets": t.operators,
                    "systems": "+".join(r["system"] for r in t.rides),
                    "stops": t.ride_stops,
                    "road_km": round(km, 1),
                    "taxi_clear": round(taxi),
                    "taxi_rush": round(taxi + km * RUSH_MIN_PER_KM * 3),
                })
        return pd.DataFrame(rows)
