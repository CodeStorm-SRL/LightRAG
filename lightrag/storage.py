import asyncio
import html
import os
from dataclasses import dataclass
from typing import Any, Union, cast
import networkx as nx
import numpy as np
from nano_vectordb import NanoVectorDB

from .utils import load_json, logger, write_json
from .base import (
    BaseGraphStorage,
    BaseKVStorage,
    BaseVectorStorage,
)

import chromadb
import json

@dataclass
class JsonKVStorage(BaseKVStorage):
    def __post_init__(self):
        working_dir = self.global_config["working_dir"]
        self._file_name = os.path.join(working_dir, f"kv_store_{self.namespace}.json")
        self._data = load_json(self._file_name) or {}
        logger.info(f"Load KV {self.namespace} with {len(self._data)} data")

    async def all_keys(self) -> list[str]:
        return list(self._data.keys())

    async def index_done_callback(self):
        write_json(self._data, self._file_name)

    async def get_by_id(self, id):
        return self._data.get(id, None)

    async def get_by_ids(self, ids, fields=None):
        if fields is None:
            return [self._data.get(id, None) for id in ids]
        return [
            (
                {k: v for k, v in self._data[id].items() if k in fields}
                if self._data.get(id, None)
                else None
            )
            for id in ids
        ]

    async def filter_keys(self, data: list[str]) -> set[str]:
        return set([s for s in data if s not in self._data])

    async def upsert(self, data: dict[str, dict]):
        left_data = {k: v for k, v in data.items() if k not in self._data}
        self._data.update(left_data)
        return left_data

    async def drop(self):
        self._data = {}

@dataclass
class DuckDBStorage(BaseKVStorage):
    import duckdb

    def __post_init__(self):
        working_dir = self.global_config["working_dir"]
        self._file_name = os.path.join(working_dir, f"kv_store_{self.namespace}.db")
        self._conn = self.duckdb.connect(self._file_name, read_only=False)
        self._conn.execute("""
        CREATE TABLE IF NOT EXISTS kv_store (
            key VARCHAR PRIMARY KEY,
            value JSON
        )
        """)
        self._data = self._conn.execute("SELECT * FROM kv_store").fetchall()
        logger.info(f"Load DuckDB {self._file_name} with {len(self._data)} data")

    async def all_keys(self) -> list[str]:
        keys = [row[0] for row in self._conn.execute("SELECT key FROM kv_store").fetchall()]
        return keys

    async def index_done_callback(self):
        pass

    async def get_by_id(self, id):
        result = self._conn.execute("SELECT value FROM kv_store WHERE key = ?", (id,)).fetchone()
        # convert to dict
        ret = json.loads(result[0]) if result else None
        return ret

    async def get_by_ids(self, ids, fields=None):
        placeholders = ', '.join(['?'] * len(ids))
        results = self._conn.execute(f"SELECT key, value FROM kv_store WHERE key IN ({placeholders})", ids).fetchall()

        # Optionally filter fields
        if fields:
            filtered_results = [{field: item[field] for field in fields if field in item} for _, item in results]
            return dict(zip(ids, filtered_results))
        return dict(results)

    async def filter_keys(self, data: list[str]) -> set[str]:
        return set([s for s in data if s not in self._data])

    async def upsert(self, data: dict[str, dict]):
        # Insert or update records
        for key, value in data.items():
            self._conn.execute("""
            INSERT INTO kv_store (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """, (key, value))
        
        # Return keys that were newly added (not updated)
        existing_keys = await self.filter_keys(list(data.keys()))
        left_data = {k: v for k, v in data.items() if k not in existing_keys}
        return left_data

    async def drop(self):
        self._conn.execute("DROP TABLE IF EXISTS kv_store")
        self._conn.close()

@dataclass
class NanoVectorDBStorage(BaseVectorStorage):
    cosine_better_than_threshold: float = 0.2

    def __post_init__(self):
        self._client_file_name = os.path.join(
            self.global_config["working_dir"], f"vdb_{self.namespace}.json"
        )
        self._max_batch_size = self.global_config["embedding_batch_num"]
        self._client = NanoVectorDB(
            self.embedding_func.embedding_dim, storage_file=self._client_file_name
        )
        self.cosine_better_than_threshold = self.global_config.get(
            "cosine_better_than_threshold", self.cosine_better_than_threshold
        )

    async def upsert(self, data: dict[str, dict]):
        logger.info(f"Inserting {len(data)} vectors to {self.namespace}")
        if not len(data):
            logger.warning("You insert an empty data to vector DB")
            return []
        list_data = [
            {
                "__id__": k,
                **{k1: v1 for k1, v1 in v.items() if k1 in self.meta_fields},
            }
            for k, v in data.items()
        ]
        contents = [v["content"] for v in data.values()]
        batches = [
            contents[i : i + self._max_batch_size]
            for i in range(0, len(contents), self._max_batch_size)
        ]
        embeddings_list = await asyncio.gather(
            *[self.embedding_func(batch) for batch in batches]
        )
        embeddings = np.concatenate(embeddings_list)
        for i, d in enumerate(list_data):
            d["__vector__"] = embeddings[i]
        results = self._client.upsert(datas=list_data)
        return results

    async def query(self, query: str, top_k=5):
        embedding = await self.embedding_func([query])
        embedding = embedding[0]
        results = self._client.query(
            query=embedding,
            top_k=top_k,
            better_than_threshold=self.cosine_better_than_threshold,
        )
        results = [
            {**dp, "id": dp["__id__"], "distance": dp["__metrics__"]} for dp in results
        ]
        return results

    async def index_done_callback(self):
        self._client.save()

@dataclass
class ChromaDBStorage(BaseVectorStorage):
    _max_batch_size: int = 0

    def __post_init__(self):
        self._client = chromadb.HttpClient()
        self._max_batch_size = self.global_config["embedding_batch_num"]

    async def upsert(self, data: dict[str, dict]):
        logger.info(f"Inserting {len(data)} vectors to {self.namespace}")
        if not len(data):
            logger.warning("You insert an empty data to vector DB")
            return []
        
        try:
            collection = self._client.get_or_create_collection(name=self.namespace, metadata={"hnsw:space": "cosine"})
        except Exception as e:
            logger.error(f"Failed to get or create Chroma collection: {e}")
            return []

        # Extract metadata, ids, and contents
        ids = list(data.keys())
        list_data = [
            {
                "__id__": k,
                **{k1: v1 for k1, v1 in v.items() if k1 in self.meta_fields},
            }
            for k, v in data.items()
        ]
        contents = [v["content"] for v in data.values()]
        # metadatas = [{k1: v1 for k1, v1 in v.items() if k1 in self.meta_fields} for v in data.values()]

        batches = [
            contents[i : i + self._max_batch_size]
            for i in range(0, len(contents), self._max_batch_size)
        ]

        # get all existing ids
        existing_docs = collection.get()
        if len(existing_docs) > 0:
            existing_ids = existing_docs['ids']
            missing_ids = [id for id in ids if id not in existing_ids]
            if len(missing_ids) == 0:
                logger.info(f"No new data to upsert.")
                return ids

        ids = missing_ids

        embeddings_list = await asyncio.gather(
            *[self.embedding_func(batch) for batch in batches]
        )
        embeddings = np.concatenate(embeddings_list)
        
        # Add data to ChromaDB collection
        try:
            if len(self.meta_fields) > 0:
                for i, d in enumerate(list_data):
                    d.pop("__id__", None)
                    document = json.dumps(d)
                    collection.upsert(
                        ids=[ids[i]],
                        documents=[document],
                        # metadatas=metadatas, #TODO: re-implement metadatas
                        embeddings=[embeddings[i]]
                    )
            else:
                collection.upsert(
                    ids=ids,
                    documents=contents,
                    embeddings=embeddings
                )
        except Exception as e:
            logger.error(f"Failed to upsert data to ChromaDB: {e}")
            return []

        logger.info(f"Successfully upserted {len(list_data)} records.")
        return ids

    async def query(self, query: str, top_k=5):
        collection = self._client.get_collection(name=self.namespace)
        embedding = await self.embedding_func([query])
        embedding = embedding[0]
        results = collection.query(
            query_embeddings=embedding,
            n_results=top_k,
            include=['documents', 'distances', 'metadatas', 'embeddings']
        )

        distances = results['distances'][0]
        ids = results['ids'][0]
        documents = results['documents'][0]

        ret = []
        for i in range(len(ids)):
            keys = []
            try: 
                d = json.loads(documents[i])
                keys = list(d.keys())
            except:
                d = documents[i]
            res = {
                'id': ids[i],
                'distance': distances[i],
                '__id__': ids[i],
                '__metrics__': distances[i],
            }
            for k in keys:
                res[k] = d[k]
            ret.append(res)

        return ret

@dataclass
class NetworkXStorage(BaseGraphStorage):
    @staticmethod
    def load_nx_graph(file_name) -> nx.Graph:
        if os.path.exists(file_name):
            return nx.read_graphml(file_name)
        return None

    @staticmethod
    def write_nx_graph(graph: nx.Graph, file_name):
        logger.info(
            f"Writing graph with {graph.number_of_nodes()} nodes, {graph.number_of_edges()} edges"
        )
        nx.write_graphml(graph, file_name)

    @staticmethod
    def stable_largest_connected_component(graph: nx.Graph) -> nx.Graph:
        """Refer to https://github.com/microsoft/graphrag/index/graph/utils/stable_lcc.py
        Return the largest connected component of the graph, with nodes and edges sorted in a stable way.
        """
        from graspologic.utils import largest_connected_component

        graph = graph.copy()
        graph = cast(nx.Graph, largest_connected_component(graph))
        node_mapping = {
            node: html.unescape(node.upper().strip()) for node in graph.nodes()
        }  # type: ignore
        graph = nx.relabel_nodes(graph, node_mapping)
        return NetworkXStorage._stabilize_graph(graph)

    @staticmethod
    def _stabilize_graph(graph: nx.Graph) -> nx.Graph:
        """Refer to https://github.com/microsoft/graphrag/index/graph/utils/stable_lcc.py
        Ensure an undirected graph with the same relationships will always be read the same way.
        """
        fixed_graph = nx.DiGraph() if graph.is_directed() else nx.Graph()

        sorted_nodes = graph.nodes(data=True)
        sorted_nodes = sorted(sorted_nodes, key=lambda x: x[0])

        fixed_graph.add_nodes_from(sorted_nodes)
        edges = list(graph.edges(data=True))

        if not graph.is_directed():

            def _sort_source_target(edge):
                source, target, edge_data = edge
                if source > target:
                    temp = source
                    source = target
                    target = temp
                return source, target, edge_data

            edges = [_sort_source_target(edge) for edge in edges]

        def _get_edge_key(source: Any, target: Any) -> str:
            return f"{source} -> {target}"

        edges = sorted(edges, key=lambda x: _get_edge_key(x[0], x[1]))

        fixed_graph.add_edges_from(edges)
        return fixed_graph

    def __post_init__(self):
        self._graphml_xml_file = os.path.join(
            self.global_config["working_dir"], f"graph_{self.namespace}.graphml"
        )
        preloaded_graph = NetworkXStorage.load_nx_graph(self._graphml_xml_file)
        if preloaded_graph is not None:
            logger.info(
                f"Loaded graph from {self._graphml_xml_file} with {preloaded_graph.number_of_nodes()} nodes, {preloaded_graph.number_of_edges()} edges"
            )
        self._graph = preloaded_graph or nx.Graph()
        self._node_embed_algorithms = {
            "node2vec": self._node2vec_embed,
        }

    async def index_done_callback(self):
        NetworkXStorage.write_nx_graph(self._graph, self._graphml_xml_file)

    async def has_node(self, node_id: str) -> bool:
        return self._graph.has_node(node_id)

    async def has_edge(self, source_node_id: str, target_node_id: str) -> bool:
        return self._graph.has_edge(source_node_id, target_node_id)

    async def get_node(self, node_id: str) -> Union[dict, None]:
        return self._graph.nodes.get(node_id)

    async def node_degree(self, node_id: str) -> int:
        return self._graph.degree(node_id)

    async def edge_degree(self, src_id: str, tgt_id: str) -> int:
        return self._graph.degree(src_id) + self._graph.degree(tgt_id)

    async def get_edge(
        self, source_node_id: str, target_node_id: str
    ) -> Union[dict, None]:
        return self._graph.edges.get((source_node_id, target_node_id))

    async def get_node_edges(self, source_node_id: str):
        if self._graph.has_node(source_node_id):
            return list(self._graph.edges(source_node_id))
        return None

    async def upsert_node(self, node_id: str, node_data: dict[str, str]):
        self._graph.add_node(node_id, **node_data)

    async def upsert_edge(
        self, source_node_id: str, target_node_id: str, edge_data: dict[str, str]
    ):
        self._graph.add_edge(source_node_id, target_node_id, **edge_data)

    async def embed_nodes(self, algorithm: str) -> tuple[np.ndarray, list[str]]:
        if algorithm not in self._node_embed_algorithms:
            raise ValueError(f"Node embedding algorithm {algorithm} not supported")
        return await self._node_embed_algorithms[algorithm]()

    async def _node2vec_embed(self):
        from graspologic import embed

        embeddings, nodes = embed.node2vec_embed(
            self._graph,
            **self.global_config["node2vec_params"],
        )

        nodes_ids = [self._graph.nodes[node_id]["id"] for node_id in nodes]
        return embeddings, nodes_ids
