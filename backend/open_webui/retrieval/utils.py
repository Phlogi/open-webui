import logging
import os
from typing import Optional, Union
from concurrent.futures import ThreadPoolExecutor

import requests
import hashlib

from huggingface_hub import snapshot_download
from langchain.retrievers import ContextualCompressionRetriever, EnsembleRetriever
from langchain_community.retrievers import BM25Retriever
from langchain_core.documents import Document


from open_webui.config import VECTOR_DB
from open_webui.retrieval.vector.connector import VECTOR_DB_CLIENT
from open_webui.utils.misc import get_last_user_message, calculate_sha256_string

from open_webui.models.users import UserModel
from open_webui.models.files import Files

from open_webui.env import (
    SRC_LOG_LEVELS,
    OFFLINE_MODE,
    ENABLE_FORWARD_USER_INFO_HEADERS,
)

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["RAG"])


from typing import Any

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.retrievers import BaseRetriever


class VectorSearchRetriever(BaseRetriever):
    collection_name: Any
    embedding_function: Any
    top_k: int

    def _get_relevant_documents(
        self,
        query: str,
        *,
        run_manager: CallbackManagerForRetrieverRun,
    ) -> list[Document]:
        result = VECTOR_DB_CLIENT.search(
            collection_name=self.collection_name,
            vectors=[self.embedding_function(query)],
            limit=self.top_k,
        )

        ids = result.ids[0]
        metadatas = result.metadatas[0]
        documents = result.documents[0]

        results = []
        for idx in range(len(ids)):
            results.append(
                Document(
                    metadata=metadatas[idx],
                    page_content=documents[idx],
                )
            )
        return results


def query_doc(
    collection_name: str, query_embedding: list[float], k: int, user: UserModel = None
):
    try:
        result = VECTOR_DB_CLIENT.search(
            collection_name=collection_name,
            vectors=[query_embedding],
            limit=k,
        )

        if result:
            log.info(f"query_doc:result {result.ids} {result.metadatas}")

        return result
    except Exception as e:
        log.exception(f"Error querying doc {collection_name} with limit {k}: {e}")
        raise e


def get_doc(collection_name: str, user: UserModel = None):
    try:
        result = VECTOR_DB_CLIENT.get(collection_name=collection_name)

        if result:
            log.info(f"query_doc:result {result.ids} {result.metadatas}")

        return result
    except Exception as e:
        log.exception(f"Error getting doc {collection_name}: {e}")
        raise e


def query_doc_with_hybrid_search(
    collection_name: str,
    collection_data,
    query: str,
    embedding_function,
    k: int,
    reranking_function,
    k_reranker: int,
    r: float,
) -> dict:
    try:
        def create_bm25_retriever():
            retriever = BM25Retriever.from_texts(
                texts=collection_data.documents[0],
                metadatas=collection_data.metadatas[0],
            )
            retriever.k = k
            return retriever

        def create_vector_retriever():
            return VectorSearchRetriever(
                collection_name=collection_name,
                embedding_function=embedding_function,
                top_k=k,
            )

        # Execute retriever creation in parallel
        with ThreadPoolExecutor(max_workers=2) as executor:
            bm25_future = executor.submit(create_bm25_retriever)
            vector_future = executor.submit(create_vector_retriever)

            # Get results from futures
            bm25_retriever = bm25_future.result()
            vector_search_retriever = vector_future.result()

        ensemble_retriever = EnsembleRetriever(
            retrievers=[bm25_retriever, vector_search_retriever], weights=[0.5, 0.5]
        )

        compressor = RerankCompressor(
            embedding_function=embedding_function,
            top_n=k_reranker,
            reranking_function=reranking_function,
            r_score=r,
        )

        compression_retriever = ContextualCompressionRetriever(
            base_compressor=compressor, base_retriever=ensemble_retriever
        )

        collection_data = compression_retriever.invoke(query)
        collection_data = {
            "distances": [[d.metadata.get("score") for d in collection_data]],
            "documents": [[d.page_content for d in collection_data]],
            "metadatas": [[d.metadata for d in collection_data]],
        }

        log.info(
            "query_doc_with_hybrid_search:result "
            + f'{collection_data["metadatas"]} {collection_data["distances"]}'
        )

        return collection_data
    except Exception as e:
        raise e


def merge_get_results(get_results: list[dict]) -> dict:
    # Initialize lists to store combined data
    combined_documents = []
    combined_metadatas = []
    combined_ids = []

    for data in get_results:
        combined_documents.extend(data["documents"][0])
        combined_metadatas.extend(data["metadatas"][0])
        combined_ids.extend(data["ids"][0])

    # Create the output dictionary
    result = {
        "documents": [combined_documents],
        "metadatas": [combined_metadatas],
        "ids": [combined_ids],
    }

    return result


def merge_and_sort_query_results(
    query_results: list[dict], k: int, reverse: bool = False
) -> dict:
    # Pre-allocate combined data structure with estimated capacity
    estimated_capacity = sum(len(data["documents"][0]) for data in query_results)
    combined = []
    combined.reserve(estimated_capacity) if hasattr(list, 'reserve') else None  
    
    seen_hashes = {} 
    
    # Process all results in a single pass
    for data in query_results:
        distances = data["distances"][0]
        documents = data["documents"][0]
        metadatas = data["metadatas"][0]

        # Pre-compute document hashes in batch if all are strings
        if all(isinstance(doc, str) for doc in documents):
            for distance, document, metadata in zip(distances, documents, metadatas):
                doc_hash = hashlib.md5(document.encode()).hexdigest()
                
                if doc_hash not in seen_hashes:
                    seen_hashes[doc_hash] = True
                    combined.append((distance, document, metadata))
        else:
            # Fallback for non-string documents
            for distance, document, metadata in zip(distances, documents, metadatas):
                if isinstance(document, str):
                    doc_hash = hashlib.md5(document.encode()).hexdigest()
                    
                    if doc_hash not in seen_hashes:
                        seen_hashes[doc_hash] = True
                        combined.append((distance, document, metadata))
    
    # Early return for empty results
    if not combined:
        return {
            "distances": [[]],
            "documents": [[]],
            "metadatas": [[]],
        }
    
    combined.sort(key=lambda x: x[0], reverse=reverse)
    
    # Truncate to top k
    del combined[k:]
    
    # Unzip the results using zip with * operator (more efficient than multiple list comprehensions)
    sorted_distances, sorted_documents, sorted_metadatas = zip(*combined) if combined else ([], [], [])
    
    return {
        "distances": [list(sorted_distances)],
        "documents": [list(sorted_documents)],
        "metadatas": [list(sorted_metadatas)],
    }


def get_all_items_from_collections(collection_names: list[str]) -> dict:
    results = []

    for collection_name in collection_names:
        if collection_name:
            try:
                result = get_doc(collection_name=collection_name)
                if result is not None:
                    results.append(result.model_dump())
            except Exception as e:
                log.exception(f"Error when querying the collection: {e}")
        else:
            pass

    return merge_get_results(results)


def query_collection(
    collection_names: list[str],
    queries: list[str],
    embedding_function,
    k: int,
) -> dict:
    results = []
    for query in queries:
        query_embedding = embedding_function(query)
        for collection_name in collection_names:
            if collection_name:
                try:
                    result = query_doc(
                        collection_name=collection_name,
                        k=k,
                        query_embedding=query_embedding,
                    )
                    if result is not None:
                        results.append(result.model_dump())
                except Exception as e:
                    log.exception(f"Error when querying the collection: {e}")
            else:
                pass

    if VECTOR_DB == "chroma":
        # Chroma uses unconventional cosine similarity, so we don't need to reverse the results
        # https://docs.trychroma.com/docs/collections/configure#configuring-chroma-collections
        return merge_and_sort_query_results(results, k=k, reverse=False)
    else:
        return merge_and_sort_query_results(results, k=k, reverse=True)


def query_collection_with_hybrid_search(
    collection_names: list[str],
    queries: list[str],
    embedding_function,
    k: int,
    reranking_function,
    k_reranker: int,
    r: float,
) -> dict:
    results = []
    error = False

    # Fetch collection data once per collection
    collection_data = {}
    with ThreadPoolExecutor() as executor:
        future_results = {
            collection_name: executor.submit(VECTOR_DB_CLIENT.get, collection_name=collection_name)
            for collection_name in collection_names
        }

        # Retrieve collection data
        for collection_name, future in future_results.items():
            try:
                collection_data[collection_name] = future.result()
            except Exception as e:
                log.exception(f"Failed to fetch collection {collection_name}: {e}")
                collection_data[collection_name] = None

    def process_query(collection_name, query):
        try:
            # Fetch pre-loaded collection data
            if collection_data[collection_name] is None:
                raise Exception(f"Collection data for {collection_name} is unavailable.")

            result = query_doc_with_hybrid_search(
                collection_name=collection_name,
                collection_data=collection_data[collection_name],
                query=query,
                embedding_function=embedding_function,
                k=k,
                reranking_function=reranking_function,
                k_reranker=k_reranker,
                r=r,
            )
            return result, None
        except Exception as e:
            log.exception(f"Error when querying the collection with hybrid_search: {e}")
            return None, e

    tasks = [(collection_name, query) for collection_name in collection_names for query in queries]

    with ThreadPoolExecutor() as executor:
        future_results = [executor.submit(process_query, cn, q) for cn, q in tasks]
        task_results = [future.result() for future in future_results]

    for result, err in task_results:
        if err is not None:
            error = True
        elif result is not None:
            results.append(result)

    if error and not results:
        raise Exception("Hybrid search failed for all collections. Using Non-hybrid search as fallback.")

    if VECTOR_DB == "chroma":
        return merge_and_sort_query_results(results, k=k, reverse=False)
    else:
        return merge_and_sort_query_results(results, k=k, reverse=True)


def get_embedding_function(
    embedding_engine,
    embedding_model,
    embedding_function,
    url,
    key,
    embedding_batch_size,
):
    if embedding_engine == "":
        return lambda query, user=None: embedding_function.encode(query).tolist()
    elif embedding_engine in ["ollama", "openai"]:
        func = lambda query, user=None: generate_embeddings(
            engine=embedding_engine,
            model=embedding_model,
            text=query,
            url=url,
            key=key,
            user=user,
        )

        def generate_multiple(query, user, func):
            if isinstance(query, list):
                embeddings = []
                for i in range(0, len(query), embedding_batch_size):
                    embeddings.extend(
                        func(query[i : i + embedding_batch_size], user=user)
                    )
                return embeddings
            else:
                return func(query, user)

        return lambda query, user=None: generate_multiple(query, user, func)
    else:
        raise ValueError(f"Unknown embedding engine: {embedding_engine}")


def get_sources_from_files(
    request,
    files,
    queries,
    embedding_function,
    k,
    reranking_function,
    k_reranker,
    r,
    hybrid_search,
    full_context=False,
):
    log.debug(
        f"files: {files} {queries} {embedding_function} {reranking_function} {full_context}"
    )

    extracted_collections = []
    relevant_contexts = []

    for file in files:

        context = None
        if file.get("docs"):
            # BYPASS_WEB_SEARCH_EMBEDDING_AND_RETRIEVAL
            context = {
                "documents": [[doc.get("content") for doc in file.get("docs")]],
                "metadatas": [[doc.get("metadata") for doc in file.get("docs")]],
            }
        elif file.get("context") == "full":
            # Manual Full Mode Toggle
            context = {
                "documents": [[file.get("file").get("data", {}).get("content")]],
                "metadatas": [[{"file_id": file.get("id"), "name": file.get("name")}]],
            }
        elif (
            file.get("type") != "web_search"
            and request.app.state.config.BYPASS_EMBEDDING_AND_RETRIEVAL
        ):
            # BYPASS_EMBEDDING_AND_RETRIEVAL
            if file.get("type") == "collection":
                file_ids = file.get("data", {}).get("file_ids", [])

                documents = []
                metadatas = []
                for file_id in file_ids:
                    file_object = Files.get_file_by_id(file_id)

                    if file_object:
                        documents.append(file_object.data.get("content", ""))
                        metadatas.append(
                            {
                                "file_id": file_id,
                                "name": file_object.filename,
                                "source": file_object.filename,
                            }
                        )

                context = {
                    "documents": [documents],
                    "metadatas": [metadatas],
                }

            elif file.get("id"):
                file_object = Files.get_file_by_id(file.get("id"))
                if file_object:
                    context = {
                        "documents": [[file_object.data.get("content", "")]],
                        "metadatas": [
                            [
                                {
                                    "file_id": file.get("id"),
                                    "name": file_object.filename,
                                    "source": file_object.filename,
                                }
                            ]
                        ],
                    }
            elif file.get("file").get("data"):
                context = {
                    "documents": [[file.get("file").get("data", {}).get("content")]],
                    "metadatas": [
                        [file.get("file").get("data", {}).get("metadata", {})]
                    ],
                }
        else:
            collection_names = []
            if file.get("type") == "collection":
                if file.get("legacy"):
                    collection_names = file.get("collection_names", [])
                else:
                    collection_names.append(file["id"])
            elif file.get("collection_name"):
                collection_names.append(file["collection_name"])
            elif file.get("id"):
                if file.get("legacy"):
                    collection_names.append(f"{file['id']}")
                else:
                    collection_names.append(f"file-{file['id']}")

            collection_names = set(collection_names).difference(extracted_collections)
            if not collection_names:
                log.debug(f"skipping {file} as it has already been extracted")
                continue

            if full_context:
                try:
                    context = get_all_items_from_collections(collection_names)
                except Exception as e:
                    log.exception(e)

            else:
                try:
                    context = None
                    if file.get("type") == "text":
                        context = file["content"]
                    else:
                        if hybrid_search:
                            try:
                                context = query_collection_with_hybrid_search(
                                    collection_names=collection_names,
                                    queries=queries,
                                    embedding_function=embedding_function,
                                    k=k,
                                    reranking_function=reranking_function,
                                    k_reranker=k_reranker,
                                    r=r,
                                )
                            except Exception as e:
                                log.debug(
                                    "Error when using hybrid search, using"
                                    " non hybrid search as fallback."
                                )

                        if (not hybrid_search) or (context is None):
                            context = query_collection(
                                collection_names=collection_names,
                                queries=queries,
                                embedding_function=embedding_function,
                                k=k,
                            )
                except Exception as e:
                    log.exception(e)

            extracted_collections.extend(collection_names)

        if context:
            if "data" in file:
                del file["data"]

            relevant_contexts.append({**context, "file": file})

    sources = []
    for context in relevant_contexts:
        try:
            if "documents" in context:
                if "metadatas" in context:
                    source = {
                        "source": context["file"],
                        "document": context["documents"][0],
                        "metadata": context["metadatas"][0],
                    }
                    if "distances" in context and context["distances"]:
                        source["distances"] = context["distances"][0]

                    sources.append(source)
        except Exception as e:
            log.exception(e)

    return sources


def get_model_path(model: str, update_model: bool = False):
    # Construct huggingface_hub kwargs with local_files_only to return the snapshot path
    cache_dir = os.getenv("SENTENCE_TRANSFORMERS_HOME")

    local_files_only = not update_model

    if OFFLINE_MODE:
        local_files_only = True

    snapshot_kwargs = {
        "cache_dir": cache_dir,
        "local_files_only": local_files_only,
    }

    log.debug(f"model: {model}")
    log.debug(f"snapshot_kwargs: {snapshot_kwargs}")

    # Inspiration from upstream sentence_transformers
    if (
        os.path.exists(model)
        or ("\\" in model or model.count("/") > 1)
        and local_files_only
    ):
        # If fully qualified path exists, return input, else set repo_id
        return model
    elif "/" not in model:
        # Set valid repo_id for model short-name
        model = "sentence-transformers" + "/" + model

    snapshot_kwargs["repo_id"] = model

    # Attempt to query the huggingface_hub library to determine the local path and/or to update
    try:
        model_repo_path = snapshot_download(**snapshot_kwargs)
        log.debug(f"model_repo_path: {model_repo_path}")
        return model_repo_path
    except Exception as e:
        log.exception(f"Cannot determine model snapshot path: {e}")
        return model


def generate_openai_batch_embeddings(
    model: str,
    texts: list[str],
    url: str = "https://api.openai.com/v1",
    key: str = "",
    user: UserModel = None,
) -> Optional[list[list[float]]]:
    try:
        r = requests.post(
            f"{url}/embeddings",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
                **(
                    {
                        "X-OpenWebUI-User-Name": user.name,
                        "X-OpenWebUI-User-Id": user.id,
                        "X-OpenWebUI-User-Email": user.email,
                        "X-OpenWebUI-User-Role": user.role,
                    }
                    if ENABLE_FORWARD_USER_INFO_HEADERS and user
                    else {}
                ),
            },
            json={"input": texts, "model": model},
        )
        r.raise_for_status()
        data = r.json()
        if "data" in data:
            return [elem["embedding"] for elem in data["data"]]
        else:
            raise "Something went wrong :/"
    except Exception as e:
        log.exception(f"Error generating openai batch embeddings: {e}")
        return None


def generate_ollama_batch_embeddings(
    model: str, texts: list[str], url: str, key: str = "", user: UserModel = None
) -> Optional[list[list[float]]]:
    try:
        r = requests.post(
            f"{url}/api/embed",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
                **(
                    {
                        "X-OpenWebUI-User-Name": user.name,
                        "X-OpenWebUI-User-Id": user.id,
                        "X-OpenWebUI-User-Email": user.email,
                        "X-OpenWebUI-User-Role": user.role,
                    }
                    if ENABLE_FORWARD_USER_INFO_HEADERS
                    else {}
                ),
            },
            json={"input": texts, "model": model},
        )
        r.raise_for_status()
        data = r.json()

        if "embeddings" in data:
            return data["embeddings"]
        else:
            raise "Something went wrong :/"
    except Exception as e:
        log.exception(f"Error generating ollama batch embeddings: {e}")
        return None


def generate_embeddings(engine: str, model: str, text: Union[str, list[str]], **kwargs):
    url = kwargs.get("url", "")
    key = kwargs.get("key", "")
    user = kwargs.get("user")

    if engine == "ollama":
        if isinstance(text, list):
            embeddings = generate_ollama_batch_embeddings(
                **{"model": model, "texts": text, "url": url, "key": key, "user": user}
            )
        else:
            embeddings = generate_ollama_batch_embeddings(
                **{
                    "model": model,
                    "texts": [text],
                    "url": url,
                    "key": key,
                    "user": user,
                }
            )
        return embeddings[0] if isinstance(text, str) else embeddings
    elif engine == "openai":
        if isinstance(text, list):
            embeddings = generate_openai_batch_embeddings(model, text, url, key, user)
        else:
            embeddings = generate_openai_batch_embeddings(model, [text], url, key, user)

        return embeddings[0] if isinstance(text, str) else embeddings


import operator
from typing import Optional, Sequence

from langchain_core.callbacks import Callbacks
from langchain_core.documents import BaseDocumentCompressor, Document


class RerankCompressor(BaseDocumentCompressor):
    embedding_function: Any
    top_n: int
    reranking_function: Any
    r_score: float

    class Config:
        extra = "forbid"
        arbitrary_types_allowed = True

    def compress_documents(
        self,
        documents: Sequence[Document],
        query: str,
        callbacks: Optional[Callbacks] = None,
    ) -> Sequence[Document]:
        reranking = self.reranking_function is not None

        if reranking:
            scores = self.reranking_function.predict(
                [(query, doc.page_content) for doc in documents]
            )
        else:
            from sentence_transformers import util

            query_embedding = self.embedding_function(query)
            document_embedding = self.embedding_function(
                [doc.page_content for doc in documents]
            )
            scores = util.cos_sim(query_embedding, document_embedding)[0]

        docs_with_scores = list(zip(documents, scores.tolist()))
        if self.r_score:
            docs_with_scores = [
                (d, s) for d, s in docs_with_scores if s >= self.r_score
            ]

        result = sorted(docs_with_scores, key=operator.itemgetter(1), reverse=True)
        final_results = []
        for doc, doc_score in result[: self.top_n]:
            metadata = doc.metadata
            metadata["score"] = doc_score
            doc = Document(
                page_content=doc.page_content,
                metadata=metadata,
            )
            final_results.append(doc)
        return final_results
