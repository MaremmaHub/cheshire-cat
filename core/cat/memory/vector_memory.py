import os
import sys
import socket
from typing import Any

from cat.log import log
from qdrant_client import QdrantClient
from langchain.embeddings.base import Embeddings
from langchain.vectorstores import Qdrant
from qdrant_client.http.models import (Distance, VectorParams,  SearchParams, 
                                    ScalarQuantization, ScalarQuantizationConfig, ScalarType, QuantizationSearchParams, 
                                    CreateAliasOperation, CreateAlias)


class VectorMemory:

    local_vector_db = None

    def __init__(self, cat, verbose=False) -> None:
        self.verbose = verbose

        # Get embedder from Cat instance
        self.embedder = cat.embedder

        if self.embedder is None:
            raise Exception("No embedder passed to VectorMemory")

        self.connect_to_vector_memory()

        # get current embedding size (langchain classes do not store it)
        # TODO: move the embedder size in create collection
        self.embedder_size = len(cat.embedder.embed_query("hello world"))

        # Create vector collections
        # - Episodic memory will contain user and eventually cat utterances
        # - Declarative memory will contain uploaded documents' content (and summaries)
        # - Procedural memory will contain tools and knowledge on how to do things
        self.collections = {}
        for collection_name in ["episodic", "declarative", "procedural"]:

            # Instantiate collection
            collection = VectorMemoryCollection(
                cat=cat,
                client=self.vector_db,
                collection_name=collection_name,
                embeddings=self.embedder,
                vector_size=self.embedder_size,
            )

            # Update dictionary containing all collections
            # Useful for cross-searching and to create/use collections from plugins
            self.collections[collection_name] = collection

            # Have the collection as an instance attribute
            # (i.e. do things like cat.memory.vectors.declarative.something())
            setattr(self, collection_name, collection)

    def connect_to_vector_memory(self) -> None:
        db_path = "local_vector_memory/"
        qdrant_host = os.getenv("QDRANT_HOST", db_path)

        if len(qdrant_host) == 0 or qdrant_host == db_path:
            log(f"Qdrant path: {db_path}","INFO")
            # Qdrant local vector DB client
            
            # reconnect only if it's the first boot and not a reload
            if VectorMemory.local_vector_db is None:
                VectorMemory.local_vector_db = QdrantClient(path=db_path)
                
            self.vector_db = VectorMemory.local_vector_db
        else:
            qdrant_port = int(os.getenv("QDRANT_PORT", 6333))

            log(f"Qdrant host: {qdrant_host}","INFO")
            log(f"Qdrant port: {qdrant_port}","INFO")

            try:
                s = socket.socket()
                s.connect((qdrant_host, qdrant_port))
            except Exception:
                log("QDrant does not respond to %s:%s" %
                    (qdrant_host, qdrant_port), "ERROR")
                sys.exit()
            finally:
                s.close()

            # Qdrant vector DB client
            self.vector_db = QdrantClient(
                host=qdrant_host,
                port=qdrant_port,
            )


class VectorMemoryCollection(Qdrant):

    def __init__(self, cat, client: Any, collection_name: str, embeddings: Embeddings, vector_size: int):

        super().__init__(client, collection_name, embeddings)

        # Get a Cat instance
        self.cat = cat

        # Set embedder name for aliases
        if hasattr(self.cat.embedder, 'model'):
            self.embedder_name = self.cat.embedder.model
        elif hasattr(self.cat.embedder, 'repo_id'):
            self.embedder_name = self.cat.embedder.repo_id
        else:
            self.embedder_name = "default_embedder"

        # Set embedding size (may be changed at runtime)
        self.embedder_size = vector_size

        # Check if memory collection exists, otherwise create it
        self.create_collection_if_not_exists()

    def create_collection_if_not_exists(self):
        # create collection if it does not exist
        try:
            self.client.get_collection(self.collection_name)
            log(f'Collection "{self.collection_name}" already present in vector store', "INFO")
            log(f'Collection alias: "{self.client.get_collection_aliases(self.collection_name).aliases}" ', "INFO")
            
            # having the same size does not necessarily imply being the same embedder
            # having vectors with the same size but from diffent embedder in the same vector space is wrong
            same_size = (self.client.get_collection(self.collection_name).config.params.vectors.size==self.embedder_size)
            alias = self.embedder_name + "_" + self.collection_name
            if alias==self.client.get_collection_aliases(self.collection_name).aliases[0].alias_name and same_size:
                log(f'Collection "{self.collection_name}" has the same embedder', "INFO")
            else:
                log(f'Collection "{self.collection_name}" has different embedder', "WARNING")
                # TODO: dump collection on disk before deleting, so it can be recovered
                self.client.delete_collection(self.collection_name)
                log(f'Collection "{self.collection_name}" deleted', "WARNING")
                self.create_collection()
        except:
            self.create_collection()

        log(f"Collection {self.collection_name}:", "INFO")
        log(dict(self.client.get_collection(self.collection_name)), "INFO")

    # create collection
    def create_collection(self):

        self.cat.mad_hatter.execute_hook('before_collection_created', self)

        log(f"Creating collection {self.collection_name} ...", "WARNING")
        self.client.recreate_collection(
            collection_name=self.collection_name,
            vectors_config=VectorParams(
                size=self.embedder_size, distance=Distance.COSINE),
            quantization_config=ScalarQuantization(
                scalar=ScalarQuantizationConfig(
                    type=ScalarType.INT8,
                    quantile=0.75,
                    always_ram=False
                )
            )
        )
        self.client.update_collection_aliases(
            change_aliases_operations=[
                CreateAliasOperation(
                    create_alias=CreateAlias(
                        collection_name=self.collection_name,
                        alias_name=self.embedder_name + "_" + self.collection_name
                    )
                )
            ]
        )
        self.cat.mad_hatter.execute_hook('after_collection_created', self)

    # retrieve similar memories from text
    def recall_memories_from_text(self, text, metadata=None, k=5, threshold=None):
        # embed the text
        query_embedding = self.cat.embedder.embed_query(text)

        # search nearest vectors
        return self.recall_memories_from_embedding(
            query_embedding, metadata=metadata, k=k, threshold=threshold
        )
    
    # delete point in collection
    def delete_points(self, points_ids):
        res = self.client.delete(
            collection_name=self.collection_name,
            points_selector=points_ids,
        )
        return res

    # retrieve similar memories from embedding
    def recall_memories_from_embedding(self, embedding, metadata=None, k=5, threshold=None):

        # retrieve memories
        memories = self.client.search(
            collection_name=self.collection_name,
            query_vector=embedding,
            query_filter=self._qdrant_filter_from_dict(metadata),
            with_payload=True,
            with_vectors=True,
            limit=k,
            score_threshold=threshold,
            search_params=SearchParams(
                quantization=QuantizationSearchParams(
                    ignore=False,
                    rescore=True,
                    # oversampling=1.5 # Available as of v1.3.0
                )
            )
        )

        langchain_documents_from_points = [
            (
                self._document_from_scored_point(
                    m, self.content_payload_key, self.metadata_payload_key),
                m.score,
                m.vector,
                m.id
            )
            for m in memories
        ]


        # we'll move out of langchain conventions soon and have our own cat Document
        #for doc, score, vector in langchain_documents_from_points:
        #    doc.lc_kwargs = None

        return langchain_documents_from_points

    # retrieve all the points in the collection
    def get_all_points(self):

        # retrieving the points
        all_points, _ = self.client.scroll(
            collection_name=self.collection_name,
            with_vectors=True,
            limit=None
        )

        return all_points