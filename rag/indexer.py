"""
栖语 (Qiyu) - RAG 自动文档索引系统
监控指定目录，自动解析文档、分块、Embedding、写入 Qdrant
"""

import os
import time
import hashlib
from pathlib import Path
from typing import List, Optional

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileCreatedEvent, FileModifiedEvent
from qdrant_client import QdrantClient
from qdrant_client.http.models import Distance, VectorParams, PointStruct
from dotenv import load_dotenv
from loguru import logger

class TextSplitter:
    """轻量文本分块器（替代 langchain RecursiveCharacterTextSplitter）"""

    def __init__(self, chunk_size: int = 512, chunk_overlap: int = 128):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def split_text(self, text: str) -> list[str]:
        text = text.strip()
        if not text:
            return []
        if len(text) <= self.chunk_size:
            return [text]
        chunks = []
        start = 0
        while start < len(text):
            end = min(start + self.chunk_size, len(text))
            chunk = text[start:end]
            if end < len(text):
                for sep in ("。", "！", "？", "；", "\n"):
                    cut = chunk.rfind(sep)
                    if cut > self.chunk_size * 0.5:
                        end = start + cut + 1
                        chunk = text[start:end]
                        break
            chunks.append(chunk)
            start = max(0, end - self.chunk_overlap)
        return chunks



load_dotenv()

QDRANT_HOST = os.getenv("QDRANT_HOST", "127.0.0.1")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))
QDRANT_RAG_COLLECTION = os.getenv("QDRANT_RAG_COLLECTION", "companion_knowledge")

RAG_WATCH_DIR = os.getenv("RAG_WATCH_DIR", "./data/uploads")
RAG_CHUNK_SIZE = int(os.getenv("RAG_CHUNK_SIZE", "512"))
RAG_CHUNK_OVERLAP = int(os.getenv("RAG_CHUNK_OVERLAP", "128"))
RAG_SCAN_INTERVAL = int(os.getenv("RAG_SCAN_INTERVAL", "60"))

# 尝试导入 Embedding 服务
try:
    from embedding.service import get_embedding_service
    EMBEDDING_AVAILABLE = True
except ImportError:
    EMBEDDING_AVAILABLE = False
    logger.warning("Embedding 服务不可用，RAG 功能受限")


class DocumentParser:
    """文档解析器"""
    
    SUPPORTED_EXTENSIONS = {".txt", ".md", ".pdf", ".docx", ".doc"}
    
    @classmethod
    def parse(cls, file_path: str) -> str:
        """解析文档，返回纯文本"""
        path = Path(file_path)
        ext = path.suffix.lower()
        
        if ext not in cls.SUPPORTED_EXTENSIONS:
            logger.warning(f"不支持的文件格式: {ext}")
            return ""
        
        try:
            if ext in (".txt", ".md"):
                return cls._parse_text(file_path)
            elif ext == ".pdf":
                return cls._parse_pdf(file_path)
            elif ext in (".docx", ".doc"):
                return cls._parse_docx(file_path)
        except Exception as e:
            logger.error(f"解析文档失败 [{file_path}]: {e}")
            return ""
        
        return ""
    
    @staticmethod
    def _parse_text(file_path: str) -> str:
        """解析纯文本/Markdown。

        编码顺序有讲究：
          1. utf-8-sig —— 先吃 Windows 记事本/PowerShell 写的 BOM，否则 BOM 会变成正文第一个字符
          2. utf-8
          3. gb18030 —— GBK/GB2312 的超集（生僻字、繁体都能覆盖），比单独试 gbk 更稳
          4. latin-1 —— 永远能成功，但**极可能是乱码**，命中时打警告，方便排查
        """
        encodings = ["utf-8-sig", "utf-8", "gb18030", "latin-1"]
        for enc in encodings:
            try:
                with open(file_path, "r", encoding=enc) as f:
                    text = f.read()
            except UnicodeDecodeError:
                continue
            except OSError as exc:
                try:
                    from loguru import logger
                    logger.warning(f"[RAG] 读取失败 {file_path}: {exc}")
                except Exception:
                    pass
                return ""
            if enc == "latin-1":
                # latin-1 是兜底，不可能是"正确"的编码；文本里出现大量高位字符就是乱码信号
                suspicious = sum(1 for ch in text[:400] if "\u00c0" <= ch <= "\u00ff")
                if suspicious > 20:
                    try:
                        from loguru import logger
                        logger.warning(
                            f"[RAG] {file_path} 只能用 latin-1 解码（疑似乱码），"
                            f"建议确认源文件编码；已按原样入库")
                    except Exception:
                        pass
            return text
        return ""
    
    @staticmethod
    def _parse_pdf(file_path: str) -> str:
        """解析 PDF"""
        try:
            from pypdf import PdfReader
            reader = PdfReader(file_path)
            texts = []
            for page in reader.pages:
                text = page.extract_text()
                if text:
                    texts.append(text)
            return "\n".join(texts)
        except ImportError:
            logger.error("未安装 pypdf，无法解析 PDF。请运行: pip install pypdf")
            return ""
    
    @staticmethod
    def _parse_docx(file_path: str) -> str:
        """解析 Word"""
        try:
            from docx import Document
            doc = Document(file_path)
            texts = []
            for para in doc.paragraphs:
                if para.text:
                    texts.append(para.text)
            return "\n".join(texts)
        except ImportError:
            logger.error("未安装 python-docx，无法解析 Word。请运行: pip install python-docx")
            return ""


class RAGIndexer:
    """RAG 文档索引器"""
    
    def __init__(self):
        self.qdrant = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
        self.text_splitter = TextSplitter(
            chunk_size=RAG_CHUNK_SIZE,
            chunk_overlap=RAG_CHUNK_OVERLAP,
        )
        self.embedding = get_embedding_service() if EMBEDDING_AVAILABLE else None
        self._ensure_collection()
    
    def _ensure_collection(self):
        """确保 Qdrant collection 存在"""
        try:
            collections = self.qdrant.get_collections().collections
            collection_names = [c.name for c in collections]
            
            if QDRANT_RAG_COLLECTION not in collection_names:
                dim = self.embedding.dimension if self.embedding else 512
                self.qdrant.create_collection(
                    collection_name=QDRANT_RAG_COLLECTION,
                    vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
                )
                logger.success(f"创建 Qdrant Collection: {QDRANT_RAG_COLLECTION}")
            else:
                logger.info(f"Qdrant Collection 已存在: {QDRANT_RAG_COLLECTION}")
        except Exception as e:
            logger.error(f"初始化 Qdrant Collection 失败: {e}")
            raise
    
    def _file_hash(self, file_path: str) -> str:
        """计算文件哈希，用于去重"""
        hasher = hashlib.md5()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                hasher.update(chunk)
        return hasher.hexdigest()
    
    def _check_exists(self, file_hash: str) -> bool:
        """检查文件是否已索引"""
        try:
            result = self.qdrant.scroll(
                collection_name=QDRANT_RAG_COLLECTION,
                scroll_filter={
                    "must": [
                        {"key": "file_hash", "match": {"value": file_hash}}
                    ]
                },
                limit=1,
            )
            return len(result[0]) > 0
        except Exception:
            return False
    
    def index_file(self, file_path: str) -> bool:
        """
        索引单个文件
        
        Returns:
            是否成功索引（新文件或更新）
        """
        if not self.embedding:
            logger.error("Embedding 服务未加载，无法索引")
            return False
        
        path = Path(file_path)
        if not path.exists():
            logger.warning(f"文件不存在: {file_path}")
            return False
        
        file_hash = self._file_hash(file_path)
        
        # 检查是否已索引
        if self._check_exists(file_hash):
            logger.info(f"文件已索引，跳过: {path.name}")
            return False
        
        # 解析文档
        logger.info(f"解析文档: {path.name}")
        text = DocumentParser.parse(file_path)
        
        if not text or not text.strip():
            logger.warning(f"文档内容为空: {path.name}")
            return False
        
        # 分块
        chunks = self.text_splitter.split_text(text)
        logger.info(f"文档分块: {path.name} -> {len(chunks)} 块")
        
        if not chunks:
            return False
        
        # Embedding
        logger.info(f"生成 Embedding: {len(chunks)} 块")
        vectors = self.embedding.encode_documents(chunks)
        
        # 写入 Qdrant
        points = []
        for i, (chunk, vector) in enumerate(zip(chunks, vectors)):
            point_id = hashlib.md5(f"{file_hash}:{i}".encode()).hexdigest()
            points.append(PointStruct(
                id=point_id,
                vector=vector,
                payload={
                    "text": chunk,
                    "file_path": str(file_path),
                    "file_name": path.name,
                    "file_hash": file_hash,
                    "chunk_index": i,
                    "total_chunks": len(chunks),
                    "indexed_at": time.time(),
                }
            ))
        
        self.qdrant.upsert(
            collection_name=QDRANT_RAG_COLLECTION,
            points=points,
        )
        
        logger.success(f"索引完成: {path.name} ({len(chunks)} 块)")
        return True
    
    def index_directory(self, directory: str, recursive: bool = True):
        """索引整个目录"""
        dir_path = Path(directory)
        if not dir_path.exists():
            logger.warning(f"目录不存在: {directory}")
            return
        
        pattern = "**/*" if recursive else "*"
        files = [
            f for f in dir_path.glob(pattern)
            if f.is_file() and f.suffix.lower() in DocumentParser.SUPPORTED_EXTENSIONS
        ]
        
        logger.info(f"扫描到 {len(files)} 个文档")
        
        indexed = 0
        skipped = 0
        for file_path in files:
            if self.index_file(str(file_path)):
                indexed += 1
            else:
                skipped += 1
        
        logger.info(f"目录索引完成: 新索引 {indexed} 个, 跳过 {skipped} 个")
        return indexed
    
    def list_documents(self, limit: int = 100) -> List[dict]:
        """列出已索引文档及分块数"""
        try:
            result = self.qdrant.scroll(
                collection_name=QDRANT_RAG_COLLECTION,
                limit=limit,
                with_payload=True,
                with_vectors=False,
            )
            docs: dict[str, dict] = {}
            for point in result[0]:
                name = point.payload.get("file_name", "unknown")
                if name not in docs:
                    docs[name] = {
                        "file_name": name,
                        "file_path": point.payload.get("file_path", ""),
                        "chunks": 0,
                    }
                docs[name]["chunks"] += 1
            return list(docs.values())
        except Exception as e:
            logger.error(f"列出文档失败: {e}")
            return []

    def search(self, query: str, top_k: int = 5) -> List[dict]:
        """语义搜索"""
        if not self.embedding:
            logger.error("Embedding 服务未加载")
            return []
        
        query_vector = self.embedding.encode_query(query)
        
        results = self.qdrant.search(
            collection_name=QDRANT_RAG_COLLECTION,
            query_vector=query_vector,
            limit=top_k,
            with_payload=True,
        )
        
        return [
            {
                "text": r.payload.get("text", ""),
                "file_name": r.payload.get("file_name", ""),
                "score": r.score,
            }
            for r in results
        ]


class FileWatcher(FileSystemEventHandler):
    """文件系统监控器"""
    
    def __init__(self, indexer: RAGIndexer):
        self.indexer = indexer
    
    def on_created(self, event):
        if not event.is_directory:
            self._handle_file(event.src_path)
    
    def on_modified(self, event):
        if not event.is_directory:
            self._handle_file(event.src_path)
    
    def _handle_file(self, file_path: str):
        path = Path(file_path)
        if path.suffix.lower() in DocumentParser.SUPPORTED_EXTENSIONS:
            logger.info(f"检测到新文件: {path.name}")
            # 延迟处理，等文件写入完成
            time.sleep(1)
            self.indexer.index_file(file_path)


def run_watcher(watch_dir: str = RAG_WATCH_DIR):
    """启动文件监控"""
    indexer = RAGIndexer()
    
    # 先索引已有文件
    logger.info(f"初始化索引目录: {watch_dir}")
    indexer.index_directory(watch_dir)
    
    # 启动监控
    observer = Observer()
    handler = FileWatcher(indexer)
    observer.schedule(handler, watch_dir, recursive=True)
    observer.start()
    
    logger.info(f"文件监控已启动: {watch_dir}")
    logger.info("将文档放入此目录即可自动索引")
    
    try:
        while True:
            time.sleep(RAG_SCAN_INTERVAL)
            # 定期全量扫描，处理可能漏掉的
            indexer.index_directory(watch_dir)
    except KeyboardInterrupt:
        observer.stop()
    
    observer.join()


if __name__ == "__main__":
    import sys
    
    indexer = RAGIndexer()
    
    if len(sys.argv) > 1:
        command = sys.argv[1]
        if command == "watch":
            run_watcher()
        elif command == "index" and len(sys.argv) > 2:
            path = sys.argv[2]
            if os.path.isdir(path):
                indexer.index_directory(path)
            else:
                indexer.index_file(path)
        elif command == "search" and len(sys.argv) > 2:
            query = sys.argv[2]
            results = indexer.search(query)
            for r in results:
                print(f"[{r['score']:.3f}] {r['file_name']}: {r['text'][:100]}...")
        else:
            print("用法: python indexer.py [watch|index <path>|search <query>]")
    else:
        print("用法: python indexer.py [watch|index <path>|search <query>]")
