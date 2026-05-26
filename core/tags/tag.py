from __future__ import annotations
from typing import Any, Dict, List, Optional, Tuple, Iterator, Callable
from dataclasses import dataclass

# Сентинел для отличия "отсутствия значения" от None
_MISSING = object()

@dataclass
class TagReference:
    """Ссылка на другой узел в дереве."""
    path: str
    is_absolute: bool = True

    def __repr__(self):
        prefix = "@" if self.is_absolute else "@self:"
        return f"{prefix}{self.path}"

class TagNode:
    """Узел дерева тегов. Может содержать значение и дочерние узлы."""
    
    def __init__(self, name: str, value: Any = _MISSING):
        self.name = name
        self.value = value
        self.children: Dict[str, TagNode] = {}

    def add_child(self, name: str) -> TagNode:
        if name not in self.children:
            self.children[name] = TagNode(name)
        return self.children[name]

    def get_child(self, name: str) -> Optional[TagNode]:
        return self.children.get(name)

    def has_value(self) -> bool:
        return self.value is not _MISSING

    def is_leaf(self) -> bool:
        return not self.children

    def __repr__(self):
        val = "MISSING" if self.value is _MISSING else self.value
        return f"TagNode(name={self.name}, value={val}, children={list(self.children.keys())})"


class TagTree:
    """Дерево тегов с поддержкой 'Composition Supremacy' (ленивое разрешение ссылок)."""
    
    SEP = ":"

    def __init__(self, root_name: str = "root"):
        self.root = TagNode(root_name)
        self.context: Optional[TagTree] = self

    def _get_path_parts(self, path: str) -> List[str]:
        if not path: return []
        return [p.strip() for p in path.split(self.SEP) if p.strip()]

    def set(self, path: str, value: Any):
        """Устанавливает значение по указанному пути."""
        parts = self._get_path_parts(path)
        current = self.root
        for part in parts:
            current = current.add_child(part)
        current.value = value

    def get(self, path: str, default: Any = None, resolve_refs: bool = True) -> Any:
        """
        Возвращает значение по пути, используя ленивое разрешение ссылок и композицию.
        """
        parts = self._get_path_parts(path)
        
        # 1. Сначала пробуем найти "честно" по всему пути (с учетом промежуточных ссылок)
        val = self._get_recursive(self.root, parts, _MISSING, resolve_refs)
        if val is not _MISSING:
            return val
            
        # 2. Если не нашли, пробуем "подниматься" вверх по пути и искать ссылки у предков
        # Например, если ищем a:b:c, и у 'a' есть ссылка, то ищем 'b:c' в этой ссылке.
        current_path = []
        for i in range(len(parts)):
            current_path.append(parts[i])
            node = self.get_node(self.SEP.join(current_path))
            if node and resolve_refs and isinstance(node.value, TagReference):
                target = self.resolve_reference(node.value)
                if isinstance(target, TagTree):
                    remaining_path = self.SEP.join(parts[i+1:])
                    val = target.get(remaining_path, default=_MISSING, resolve_refs=resolve_refs)
                    if val is not _MISSING:
                        return val
        
        return default

    def _get_recursive(self, current_node: TagNode, parts: List[str], default: Any, resolve_refs: bool) -> Any:
        if not parts:
            val = current_node.value
            if val is _MISSING: return default
            if resolve_refs and isinstance(val, TagReference):
                target = self.resolve_reference(val)
                if isinstance(target, TagTree):
                    return target.get("", default=default, resolve_refs=resolve_refs)
                return target
            return val

        next_part = parts[0]
        next_node = current_node.get_child(next_part)

        if next_node:
            return self._get_recursive(next_node, parts[1:], default, resolve_refs)
        
        # Если узла нет, проверяем текущий узел на наличие ссылки
        if resolve_refs and isinstance(current_node.value, TagReference):
            target = self.resolve_reference(current_node.value)
            if isinstance(target, TagTree):
                return target.get(self.SEP.join(parts), default=default, resolve_refs=resolve_refs)

        return default

    def resolve_reference(self, ref: TagReference) -> Any:
        """Разрешает ссылку, возвращая либо значение, либо поддерево (TagTree)."""
        target_tree = self.context if ref.is_absolute else self
        if target_tree is None: return None
        
        # Пытаемся получить узел
        parts = self._get_path_parts(ref.path)
        node = target_tree.get_node(ref.path)
        
        if node:
            # Если у узла есть дети, возвращаем его как TagTree (View)
            if node.children:
                return target_tree.extract(ref.path)
            # Если детей нет, возвращаем просто значение
            return node.value if node.value is not _MISSING else None
        return None

    def get_node(self, path: str) -> Optional[TagNode]:
        parts = self._get_path_parts(path)
        current = self.root
        for part in parts:
            current = current.get_child(part)
            if current is None: return None
        return current

    def has(self, path: str) -> bool:
        return self.get_node(path) is not None

    def extract(self, path: str) -> TagTree:
        """Создает копию ветки как новое дерево."""
        source_node = self.get_node(path)
        new_tree = TagTree(root_name=source_node.name if source_node else "root")
        if source_node:
            self._clone_node(source_node, new_tree.root)
        new_tree.context = self.context
        return new_tree

    def _clone_node(self, src: TagNode, dst: TagNode):
        dst.value = src.value
        for name, child in src.children.items():
            new_child = dst.add_child(name)
            self._clone_node(child, new_child)

    def merge(self, other: TagTree, overwrite: bool = True):
        def _merge(dst: TagNode, src: TagNode):
            if src.value is not _MISSING:
                if overwrite or dst.value is _MISSING:
                    dst.value = src.value
            for name, child in src.children.items():
                _merge(dst.add_child(name), child)
        _merge(self.root, other.root)

    def to_dict(self, resolve_refs: bool = False) -> Dict[str, Any]:
        res = {}
        def _walk(node: TagNode, path: List[str]):
            if node.value is not _MISSING:
                val = node.value
                if resolve_refs and isinstance(val, TagReference):
                    val = self.resolve_reference(val)
                res[self.SEP.join(path)] = val
            for name, child in node.children.items():
                _walk(child, path + [name])
        
        for name, child in self.root.children.items():
            _walk(child, [name])
        return res

    def items(self, resolve_refs: bool = False) -> Iterator[Tuple[str, Any]]:
        """Итератор по парам (путь, значение)."""
        return iter(self.to_dict(resolve_refs=resolve_refs).items())

    def show(self, resolve_refs: bool = False):
        print(f"\n=== TAG TREE STRUCTURE ({self.root.name}) ===")
        def _display(node: TagNode, indent: str = "", is_last: bool = True, is_root: bool = False):
            if not is_root:
                marker = "|-- "
                val = node.value
                val_str = ""
                if val is not _MISSING:
                    if isinstance(val, TagReference):
                        val_str = f" -> {val}"
                        if resolve_refs:
                            # Для отладки показываем, что там внутри
                            res = self.resolve_reference(val)
                            val_str += f" (Resolved)"
                    else:
                        val_str = f" = {val}"
                print(f"{indent}{marker}{node.name}{val_str}")
                indent += "    " if is_last else "|   "
            else:
                print(".")

            children = list(node.children.values())
            for i, child in enumerate(children):
                _display(child, indent, i == len(children) - 1)
        _display(self.root, is_root=True)
        print("==========================\n")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> TagTree:
        t = cls()
        for k, v in data.items(): t.set(k, v)
        return t

    def __getitem__(self, path: str) -> Any:
        val = self.get(path, default=_MISSING)
        if val is _MISSING:
            raise KeyError(path)
        return val
