import os
from datetime import datetime

from pymongo import MongoClient
from pymongo.database import Database
from tavily import TavilyHybridClient
from tavily.databases.mongodb import MongoDB

db: Database = MongoClient(os.environ["MONGO_URI"])[os.environ["MONGO_DATABASE"]]

hybrid_rag = TavilyHybridClient(
    api_key=os.environ["TAVILY_API_KEY"],
    database=MongoDB(
        collection=db.get_collection(os.environ["MONGO_COLLECTION"]),
        index=os.environ["MONGO_INDEX"],
        embeddings_field='embeddings',
        content_field='content',
    ),
)

def save_document(document):
    if document['score'] < 0.5:
        return None # Do not save documents with low scores
    
    return {
        'content': document['content'],

         # Save the title and URL in the database
        'site_title': document['title'],
        'site_url': document['url'],

        # Add a new field
        'added_at': datetime.now()
    }

results = hybrid_rag.search("Who is Leo Messi?", save_foreign=save_document)

results = hybrid_rag.search("Where did Messi start his career?", max_results=5, max_local=5, max_foreign=5, save_foreign=True)

print(results)
