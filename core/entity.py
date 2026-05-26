from __future__ import annotations
from typing import Any, Optional, Dict
from core.tags.tag import TagTree, _MISSING

class Entity:
    """
    Сущность в мире. 
    Сама по себе не хранит логику, только данные в виде TagTree (дельта) 
    и ссылку на прототип.
    """
    
    def __init__(self, entity_id: str, prototype: Optional[TagTree] = None):
        self.id = entity_id
        self.prototype = prototype
        self.tags = TagTree()  # Локальные изменения (дельта)

    def get(self, path: str, default: Any = None) -> Any:
        """
        Многослойный поиск значения:
        1. Сначала ищем в локальной дельте (self.tags).
        2. Если там нет (_MISSING), ищем в прототипе.
        3. Если и там нет, возвращаем default.
        """
        # 1. Проверяем локальную дельту
        val = self.tags.get(path, default=_MISSING)
        if val is not _MISSING:
            return val
            
        # 2. Проверяем прототип
        if self.prototype:
            return self.prototype.get(path, default=default)
            
        return default

    def set(self, path: str, value: Any):
        """Устанавливает локальное переопределение (дельту)."""
        self.tags.set(path, value)

    def has(self, path: str) -> bool:
        """Проверяет наличие узла в дельте или прототипе."""
        return self.tags.has(path) or (self.prototype.has(path) if self.prototype else False)

    def extract(self, path: str) -> TagTree:
        """
        Создает объединенное дерево для указанного пути.
        Мерджит ветку из прототипа и дельты.
        """
        # Берем базу из прототипа
        base_tree = self.prototype.extract(path) if self.prototype else TagTree()
        # Накладываем нашу дельту поверх
        delta_branch = self.tags.extract(path)
        base_tree.merge(delta_branch, overwrite=True)
        return base_tree

    def __getitem__(self, path: str) -> Any:
        val = self.get(path, default=_MISSING)
        if val is _MISSING:
            raise KeyError(f"Tag '{path}' not found in entity {self.id}")
        return val

    def __setitem__(self, path: str, value: Any):
        self.set(path, value)

    def __repr__(self):
        proto_name = "None" if not self.prototype else "Active"
        return f"Entity(id={self.id}, prototype={proto_name}, delta_tags={len(self.tags.to_dict())})"
