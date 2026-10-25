"""
栖语 (Qiyu) - 本地 Embedding 服务
基于 sentence-transformers 的 bge-small-zh-v1.5
"""

import os
from typing import List, Union

from sentence_transformers import SentenceTransformer
from dotenv import load_dotenv
from loguru import logger

load_dotenv()

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")
EMBEDDING_DEVICE = os.getenv("EMBEDDING_DEVICE", "cpu")
EMBEDDING_DIMENSION = int(os.getenv("EMBEDDING_DIMENSION", "512"))


class EmbeddingService:
    """本地 Embedding 服务"""
    
    _instance = None
    _model = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if EmbeddingService._model is not None:
            return
        # 不在构造时加载模型：桌面版启动有 10–20 秒全耗在这里（SentenceTransformer 初始化）。
        # 改成「第一次真正编码时才加载」，并由 demo.py 的 startup 起一个后台线程预热：
        #   服务先起来 → 网页能开 → 模型在后台慢慢加载 → 用户第一次聊天时通常已经好了。
        import threading
        if getattr(EmbeddingService, "_load_lock", None) is None:
            EmbeddingService._load_lock = threading.Lock()

    def _ensure_loaded(self):
        """真正的模型加载（线程安全、只执行一次）。"""
        if EmbeddingService._model is not None:
            return
        with EmbeddingService._load_lock:
            if EmbeddingService._model is not None:
                return
            logger.info(f"加载 Embedding 模型: {EMBEDDING_MODEL}")
            logger.info(f"设备: {EMBEDDING_DEVICE}")
            try:
                EmbeddingService._model = SentenceTransformer(
                    EMBEDDING_MODEL,
                    device=EMBEDDING_DEVICE,
                    # 离线机器无法访问 huggingface：只用本地缓存，不发 HEAD 请求，
                    # 否则每次加载会重试 5 次、每次 10s 超时，把聊天链路拖死。
                    local_files_only=True,
                )
                logger.success("Embedding 模型加载完成")
                logger.info(f"维度: {EMBEDDING_DIMENSION}")
            except Exception as e:
                logger.error(f"加载 Embedding 模型失败: {e}")
                raise

    def warmup(self):
        """后台预热入口（给 startup 用）。失败只记日志，不影响服务。"""
        try:
            self._ensure_loaded()
            return True
        except Exception:
            return False

    @property
    def model(self) -> SentenceTransformer:
        if EmbeddingService._model is None:
            self._ensure_loaded()
        return EmbeddingService._model
    
    @property
    def dimension(self) -> int:
        return EMBEDDING_DIMENSION
    
    def encode(
        self,
        texts: Union[str, List[str]],
        normalize: bool = True,
    ) -> List[List[float]]:
        """
        将文本编码为向量
        
        Args:
            texts: 单条文本或文本列表
            normalize: 是否归一化（L2），用于余弦相似度
        
        Returns:
            向量列表
        """
        if isinstance(texts, str):
            texts = [texts]
        
        # 空文本过滤
        texts = [t.strip() for t in texts if t and t.strip()]
        if not texts:
            return []
        
        embeddings = self.model.encode(
            texts,
            normalize_embeddings=normalize,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        
        return embeddings.tolist()
    
    def encode_query(self, query: str) -> List[float]:
        """
        编码查询（会自动添加查询前缀）
        bge 模型建议查询添加前缀: "为这个句子生成表示: "
        """
        # bge 中文模型查询前缀
        prefixed_query = f"为这个句子生成表示: {query}"
        result = self.encode(prefixed_query, normalize=True)
        return result[0] if result else []
    
    def encode_documents(self, documents: List[str]) -> List[List[float]]:
        """
        编码文档（无特殊前缀）
        """
        return self.encode(documents, normalize=True)


# 全局单例
def get_embedding_service() -> EmbeddingService:
    return EmbeddingService()


if __name__ == "__main__":
    # 测试
    service = EmbeddingService()
    
    texts = [
        "你好，今天天气不错。",
        "人工智能正在改变世界。",
        "我喜欢吃意大利面。",
    ]
    
    vectors = service.encode_documents(texts)
    logger.info(f"编码 {len(vectors)} 条文本")
    logger.info(f"向量维度: {len(vectors[0])}")
    
    query_vec = service.encode_query("AI技术")
    logger.info(f"查询向量维度: {len(query_vec)}")
