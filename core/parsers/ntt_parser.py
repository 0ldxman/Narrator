from __future__ import annotations
import os
import sys
import operator
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Iterator, Union, Callable
from lark import Lark, Transformer, v_args, Token

# Добавляем путь для импорта TagTree
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
from core.tags.tag import TagTree, TagReference

# --- AST NODES ---

@dataclass(kw_only=True)
class ASTNode:
    line: int = 0
    column: int = 0
    file: str = "unknown"

@dataclass(kw_only=True)
class Expression(ASTNode):
    pass

@dataclass(kw_only=True)
class Literal(Expression):
    value: Any

@dataclass(kw_only=True)
class Reference(Expression):
    path: str
    is_absolute: bool = True

@dataclass(kw_only=True)
class BinaryOp(Expression):
    left: Expression
    op: str
    right: Expression

@dataclass(kw_only=True)
class TagRef(Expression):
    path: str

@dataclass(kw_only=True)
class Statement(ASTNode):
    pass

@dataclass(kw_only=True)
class Assignment(Statement):
    path: str
    expr: Expression
    body: Optional[List[Statement]] = None

@dataclass(kw_only=True)
class Block(Statement):
    path: str
    body: List[Statement]

@dataclass(kw_only=True)
class Import(ASTNode):
    path: str

@dataclass(kw_only=True)
class Definition(ASTNode):
    name: str
    parents: List[str]
    body: List[Statement]
    is_type: bool = True

# --- GRAMMAR ---

NTT_GRAMMAR = r"""
    start: (import_stmt | type_def | entity_def)*

    import_stmt: "import" ESCAPED_STRING
    
    type_def: "type" path [inheritance] block
    entity_def: "entity" path [inheritance] block
    
    inheritance: "<" path ("," path)*
    
    block: "{" statement* "}"
    
    ?statement: assignment
             | nested_block
             | assignment_with_block

    assignment: path "=" expr
    nested_block: path block
    assignment_with_block: path "=" expr block

    path: NAME (":" NAME)*

    # Expressions
    ?expr: term
         | expr "+" term   -> add
         | expr "-" term   -> sub

    ?term: factor
         | term "*" factor -> mul
         | term "/" factor -> div

    ?factor: primary
           | "(" expr ")"

    ?primary: literal
            | reference
            | path         -> tag_ref

    reference: "@" path    -> abs_ref
             | "@self:" path -> self_ref

    ?literal: ESCAPED_STRING -> string
            | SIGNED_NUMBER  -> number
            | "true"         -> true
            | "false"        -> false
            | "null"         -> null
            | DICE           -> dice
            | list

    list: "[" [expr ("," expr)*] "]"
    
    DICE.2: /\d+d\d+([+-]\d+)?/
    NAME: /[a-zA-Z_][a-zA-Z0-9_]*/
    
    %import common.ESCAPED_STRING
    %import common.SIGNED_NUMBER
    %import common.WS
    %import common.CPP_COMMENT
    %import common.C_COMMENT
    
    COMMENT: /#[^\n]*/
    
    %ignore WS
    %ignore CPP_COMMENT
    %ignore C_COMMENT
    %ignore COMMENT
"""

# --- TRANSFORMER ---

class NTTTransformer(Transformer):
    def __init__(self, file_name="unknown"):
        super().__init__()
        self.file_name = file_name

    def _meta(self, token_or_tree):
        if hasattr(token_or_tree, 'line'):
            return {"line": token_or_tree.line, "column": token_or_tree.column, "file": self.file_name}
        meta = getattr(token_or_tree, 'meta', None)
        if meta:
            return {"line": meta.line, "column": meta.column, "file": self.file_name}
        return {}

    def start(self, items): return items
    
    def import_stmt(self, args):
        path = str(args[0])[1:-1]
        return Import(path=path, **self._meta(args[0]))

    def type_def(self, args):
        name, parents, body = args[0], args[1] or [], args[-1]
        return Definition(name=str(name), parents=parents, body=body, is_type=True, **self._meta(args[0]))

    def entity_def(self, args):
        name, parents, body = args[0], args[1] or [], args[-1]
        return Definition(name=str(name), parents=parents, body=body, is_type=False, **self._meta(args[0]))

    def inheritance(self, names): return [str(n) for n in names]
    def block(self, stmts): return list(stmts)

    def assignment(self, args):
        path, expr = args
        return Assignment(path=path, expr=expr, **self._meta(expr))

    def nested_block(self, args):
        path, body = args
        return Block(path=path, body=body, **self._meta(path))

    def assignment_with_block(self, args):
        path, expr, body = args
        return Assignment(path=path, expr=expr, body=body, **self._meta(path))

    def path(self, parts): return ":".join(parts)

    # Expressions
    def add(self, args): return BinaryOp(left=args[0], op="+", right=args[1], **self._meta(args[0]))
    def sub(self, args): return BinaryOp(left=args[0], op="-", right=args[1], **self._meta(args[0]))
    def mul(self, args): return BinaryOp(left=args[0], op="*", right=args[1], **self._meta(args[0]))
    def div(self, args): return BinaryOp(left=args[0], op="/", right=args[1], **self._meta(args[0]))
    
    def tag_ref(self, args): return TagRef(path=args[0], **self._meta(args[0]))
    
    def abs_ref(self, args): return Reference(path=args[0], is_absolute=True, **self._meta(args[0]))
    def self_ref(self, args): return Reference(path=args[0], is_absolute=False, **self._meta(args[0]))

    @v_args(inline=True)
    def string(self, s): return Literal(value=str(s)[1:-1], **self._meta(s))
    
    @v_args(inline=True)
    def number(self, n):
        val = float(n) if "." in n else int(n)
        return Literal(value=val, **self._meta(n))
    
    def true(self, t): return Literal(value=True, **self._meta(t))
    def false(self, f): return Literal(value=False, **self._meta(f))
    def null(self, n): return Literal(value=None, **self._meta(n))
    def dice(self, d): return Literal(value=str(d), **self._meta(d))
    def list(self, items): return Literal(value=list(items), **self._meta(items[0] if items else Token('WS', '')))

# --- PARSER & EVALUATOR ---

class NTTEvaluator:
    OPS = {
        "+": operator.add, "-": operator.sub,
        "*": operator.mul, "/": operator.truediv
    }

    def __init__(self, tree: TagTree):
        self.tree = tree

    def get_value(self, path: str) -> Any:
        val = self.tree.get(path)
        if isinstance(val, Expression):
            return self.eval(val)
        return val

    def eval(self, expr: Expression) -> Any:
        if isinstance(expr, Literal):
            if isinstance(expr.value, list):
                return [self.eval(e) if isinstance(e, Expression) else e for e in expr.value]
            return expr.value
        
        if isinstance(expr, Reference):
            return TagReference(path=expr.path, is_absolute=expr.is_absolute)
            
        if isinstance(expr, TagRef):
            return self.get_value(expr.path)

        if isinstance(expr, BinaryOp):
            left = self.eval(expr.left)
            right = self.eval(expr.right)
            
            # Магия: авто-приведение к строке при сложении, если один из операндов - строка
            if expr.op == "+" and (isinstance(left, str) or isinstance(right, str)):
                return str(left) + str(right)
                
            try:
                return self.OPS[expr.op](left, right)
            except Exception as e:
                print(f"Error evaluating expression at {expr.file}:{expr.line}: {e}")
                return None
        return None

class NTTParser:
    def __init__(self, world: Optional[TagTree] = None):
        # Весь мир - это одно гигантское дерево
        self.world = world or TagTree("world")
        self.imported_files = set()

    def parse_text_direct(self, text: str, file_name: str = "memory") -> TagTree:
        """Парсит текст и монтирует всё в глобальное дерево мира."""
        lark_inst = Lark(NTT_GRAMMAR, parser='lalr', propagate_positions=True)
        transformer = NTTTransformer(file_name=file_name)
        ast = transformer.transform(lark_inst.parse(text))
        
        self._process_ast(ast, os.getcwd())
        return self.world

    def parse_file(self, file_path: str):
        file_path = os.path.abspath(file_path)
        if file_path in self.imported_files:
            return
        self.imported_files.add(file_path)

        with open(file_path, 'r', encoding='utf-8') as f:
            text = f.read()

        lark_inst = Lark(NTT_GRAMMAR, parser='lalr', propagate_positions=True)
        transformer = NTTTransformer(file_name=os.path.basename(file_path))
        ast = transformer.transform(lark_inst.parse(text))

        self._process_ast(ast, os.path.dirname(file_path))

    def _process_ast(self, ast: List[ASTNode], base_dir: str):
        for node in ast:
            if isinstance(node, Import):
                import_path = os.path.join(base_dir, node.path)
                self.parse_file(import_path)
            
            elif isinstance(node, Definition):
                # Определяем путь монтирования в глобальном дереве
                prefix = "type" if node.is_type else "entity"
                mount_path = f"{prefix}:{node.name}"
                
                # Создаем временное дерево для сборки
                temp_tree = TagTree(root_name=node.name)
                temp_tree.context = self.world
                
                # Типы могут использовать жесткое наследование (Hard Copy) через <
                # Сущности (entity) теперь ДОЛЖНЫ использовать композицию (@)
                for p_name in node.parents:
                    p_path = None
                    if p_name.startswith("type:") or p_name.startswith("entity:"):
                        p_path = p_name
                    elif self.world.has(f"type:{p_name}"):
                        p_path = f"type:{p_name}"
                    elif self.world.has(f"entity:{p_name}"):
                        p_path = f"entity:{p_name}"
                    else:
                        p_path = f"{prefix}:{p_name}"

                    p_node = self.world.get_node(p_path)
                    if p_node:
                        parent_tree = self.world.extract(p_path)
                        temp_tree.merge(parent_tree)
                    else:
                        print(f"Warning: Parent '{p_name}' (resolved as '{p_path}') not found for {node.name}")
                
                # Применяем тело
                self._apply_body(temp_tree, node.body)
                
                # Монтируем результат в глобальное дерево
                self._mount_tree(mount_path, temp_tree)

    def _mount_tree(self, path: str, source_tree: TagTree):
        """Монтирует дерево по указанному пути в глобальный мир."""
        for p, v in source_tree.items(resolve_refs=False):
            full_path = f"{path}:{p}" if p else path
            self.world.set(full_path, v)

    def _apply_body(self, tree: TagTree, body: List[Statement], prefix: str = ""):
        evaluator = NTTEvaluator(tree)
        for stmt in body:
            full_path = f"{prefix}:{stmt.path}" if prefix else stmt.path
            
            if isinstance(stmt, Assignment):
                val = evaluator.eval(stmt.expr)
                tree.set(full_path, val)
                if stmt.body:
                    self._apply_body(tree, stmt.body, full_path)
            
            elif isinstance(stmt, Block):
                self._apply_body(tree, stmt.body, full_path)

if __name__ == "__main__":
    parser = NTTParser()
    code = """
    type race:humanoid {
        body {
            torso = "torso"
            head {
                eye = "eye"
            }
        }
    }

    type race:humanoid:human < race:humanoid {
        identity:label = "Human"
    }

    entity john_snow {
        # Композиция! Джон НЕ человек, он ИМЕЕТ расу человека
        race = @type:race:humanoid:human {
            gender = "мужчина"
            # Переопределение внутри компонента
            body:head:eye = "blue eye"
        }
    }
    """
    
    print("Парсинг с Composition Supremacy...")
    world = parser.parse_text_direct(code)
    world.show(resolve_refs=True)
    
    # Проверка ленивого разрешения
    print(f"Джон -> Раса -> Глаз: {world.get('entity:john_snow:race:body:head:eye')}")
    print(f"Джон -> Раса -> Торс (подтянуто из шаблона): {world.get('entity:john_snow:race:body:torso')}")
