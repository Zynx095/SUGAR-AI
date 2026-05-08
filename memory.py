"""
memory.py
Manages local vector database interactions using ChromaDB (100% open source).
"""
import chromadb
import uuid
from config import CHROMA_DB_DIR

# Initialize the persistent ChromaDB client locally
try:
    chroma_client = chromadb.PersistentClient(path=CHROMA_DB_DIR)
    collection = chroma_client.get_or_create_collection(name="local_chat_memory")
except Exception as e:
    print(f"Warning: Failed to initialize ChromaDB. Error: {e}")
    collection = None

def store_memory(text: str):
    """Embeds and stores text into the Chroma vector database natively."""
    if not collection or not text:
        return
    try:
        doc_id = str(uuid.uuid4())
        collection.add(
            documents=[text],
            ids=[doc_id]
        )
    except Exception as e:
        print(f"Error storing memory: {e}")

def retrieve_memory(query: str, n_results: int = 3) -> list:
    """Retrieves the most semantically similar past conversations."""
    if not collection or not query:
        return []
    try:
        results = collection.query(
            query_texts=[query],
            n_results=n_results
        )
        if results and 'documents' in results and results['documents']:
            return results['documents'][0]
        return []
    except Exception as e:
        print(f"Error retrieving memory: {e}")
        return []