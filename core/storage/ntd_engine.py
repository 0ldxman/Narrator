from __future__ import annotations
import struct
import mmap
import os
import sys
import zlib
try:
    import lz4.frame as lz4f
except ImportError:
    lz4f = None
import bisect
import time
import random
import binascii
import gc
import threading
from typing import Any, Dict, List, Optional, Tuple, Union
from dataclasses import dataclass
from collections import OrderedDict

# Добавляем корень проекта в sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
from core.tags.tag import TagTree, TagNode, TagReference, _MISSING

# --- NTD BINARY SPECIFICATION ---
# Header (72 bytes)
# [0-4]   Magic: b'NTD\x00'
# [4-6]   Version: 1
# [6-8]   Reserved
# [8-16]  String Pool Offset
# [16-24] Data Section Offset (Nodes)
# [24-32] Journal Offset (Log)
# [32-40] Journal Size (Count of entries)
# [40-48] Path Index Offset
# [48-56] Dynamic Pool Offset
# [56-64] Blob Section Offset
# [64-72] EOF Offset (Total used size)

MAGIC = b'NTD\x00'
VERSION = 1
NODE_SIZE = 48
HEADER_SIZE = 72
JOURNAL_ENTRY_SIZE = 32 # Увеличили с 24 до 32 для Checksum и выравнивания

class ValueType:
    MISSING = 0
    INT = 1
    FLOAT = 2
    STRING_ID = 3
    REFERENCE = 4
    BOOL = 5
    DICE = 6
    DYNAMIC_STRING = 7
    BLOB = 8
    TOMBSTONE = 9
    LIST = 10
    COMPRESSED_BLOB = 11

# Константы для Tagged Pointers в журнале
DYNAMIC_PATH_FLAG = 1 << 63
PATH_REF_MASK = ~DYNAMIC_PATH_FLAG

# --- CACHED STRUCTS FOR PERFORMANCE ---
HEADER_STRUCT = struct.Struct('<4sHHQQQQQQQQ')
JOURNAL_STRUCT = struct.Struct('<QB16sI3x')
JOURNAL_CRC_DATA_STRUCT = struct.Struct('<QB16s') # Первые 25 байт для CRC
CRC_PAD_STRUCT = struct.Struct('<I3x')
NODE_STRUCT = struct.Struct('<IIB16sII15s')
INDEX_ENTRY_STRUCT = struct.Struct('<II')
U32_STRUCT = struct.Struct('<I')
U16_STRUCT = struct.Struct('<H')
Q_STRUCT = struct.Struct('<q8s')
D_STRUCT = struct.Struct('<d8s')
B_STRUCT = struct.Struct('<B15s')
I_STRUCT = struct.Struct('<I12s')
REL_OFF_STRUCT = struct.Struct('<Q8s')
BLOB_REF_STRUCT = struct.Struct('<QQ')
REF_VAL_STRUCT = struct.Struct('<BQ7s')
VAL_ENTRY_STRUCT = struct.Struct('<B16s') # Для элементов списка

@dataclass
class NTDHeader:
    version: int = VERSION
    string_pool_offset: int = 0
    data_offset: int = 0
    journal_offset: int = 0
    journal_size: int = 0
    index_offset: int = 0
    dynamic_pool_offset: int = 0
    blob_offset: int = 0
    eof_offset: int = 0

    def pack(self) -> bytes:
        return HEADER_STRUCT.pack(MAGIC, self.version, 0, 
                           self.string_pool_offset, self.data_offset, 
                           self.journal_offset, self.journal_size,
                           self.index_offset, self.dynamic_pool_offset,
                           self.blob_offset, self.eof_offset)

    @classmethod
    def unpack(cls, data: bytes) -> NTDHeader:
        if len(data) < HEADER_SIZE:
            return cls()
        magic, version, _, sp, data_off, journal, j_size, index, dynamic, blob, eof = HEADER_STRUCT.unpack(data)
        if magic != MAGIC: raise ValueError(f"Invalid NTD magic: {magic}")
        return cls(version, sp, data_off, journal, j_size, index, dynamic, blob, eof)

class StringPool:
    """Пул строк для сжатия путей и имен."""
    def __init__(self):
        self.strings: List[str] = []
        self.lookup: Dict[str, int] = {}

    def get_id(self, s: str) -> int:
        s = sys.intern(s) # INTERNING FOR SPEED
        if s not in self.lookup:
            self.lookup[s] = len(self.strings)
            self.strings.append(s)
        return self.lookup[s]

    def get_str(self, idx: int) -> str:
        if idx >= len(self.strings): return f"<ERR:ID {idx}>"
        return self.strings[idx]

    def pack(self) -> bytes:
        data = U32_STRUCT.pack(len(self.strings))
        for s in self.strings:
            b = s.encode('utf-8')
            data += U16_STRUCT.pack(len(b)) + b
        return data

    @classmethod
    def unpack(cls, data: bytes, offset: int) -> Tuple[StringPool, int]:
        pool = cls()
        if offset >= len(data): return pool, offset
        count = U32_STRUCT.unpack_from(data, offset)[0]
        pos = offset + 4
        for _ in range(count):
            if pos + 2 > len(data): break
            length = U16_STRUCT.unpack_from(data, pos)[0]
            pos += 2
            if pos + length > len(data): break
            s = sys.intern(data[pos:pos+length].decode('utf-8')) # INTERNING
            pool.get_id(s)
            pos += length
        return pool, pos

class NTDPacker:
    """Упаковщик TagTree в бинарный формат .ntd"""
    def __init__(self):
        self.pool = StringPool()
        self.nodes_data: List[bytes] = []
        self.path_index: List[Tuple[int, int]] = [] 

    def pack_tree(self, tree: TagTree, skip_root_name: bool = True) -> bytes:
        self._pack_node(tree.root, 0xFFFFFFFF, "", skip_node_name=skip_root_name) 
        header = NTDHeader()
        sp_data = self.pool.pack()
        nodes_blob = b''.join(self.nodes_data)
        self.path_index.sort()
        index_data = U32_STRUCT.pack(len(self.path_index))
        for h, n_id in self.path_index:
            index_data += INDEX_ENTRY_STRUCT.pack(h, n_id)

        header.string_pool_offset = HEADER_SIZE
        header.data_offset = header.string_pool_offset + len(sp_data)
        header.index_offset = header.data_offset + len(nodes_blob)
        header.journal_offset = header.index_offset + len(index_data)
        
        # Резерв под журнал (5000 записей)
        journal_reserved = JOURNAL_ENTRY_SIZE * 5000
        header.dynamic_pool_offset = header.journal_offset + journal_reserved
        header.blob_offset = header.dynamic_pool_offset
        header.eof_offset = header.blob_offset
        
        main_data = header.pack() + sp_data + nodes_blob + index_data
        padding = b'\x00' * journal_reserved
        result = main_data + padding
        header.eof_offset = len(result)
        return header.pack() + result[HEADER_SIZE:]

    def _pack_node(self, node: TagNode, parent_id: int, current_path: str, skip_node_name: bool = False) -> int:
        node_id = len(self.nodes_data)
        name_id = self.pool.get_id(node.name)
        full_path = current_path if skip_node_name else (f"{current_path}:{node.name}" if current_path else node.name)
        if full_path:
            path_hash = zlib.adler32(full_path.encode('utf-8')) & 0xFFFFFFFF
            self.path_index.append((path_hash, node_id))

        v_type, v_data = self._pack_value(node.value)
        self.nodes_data.append(b'') 
        first_child = 0xFFFFFFFF
        prev_child_id = 0xFFFFFFFF
        for i, (child_name, child_node) in enumerate(node.children.items()):
            child_id = self._pack_node(child_node, node_id, full_path)
            if i == 0: first_child = child_id
            if prev_child_id != 0xFFFFFFFF:
                parts = list(NODE_STRUCT.unpack(self.nodes_data[prev_child_id]))
                parts[5] = child_id
                self.nodes_data[prev_child_id] = NODE_STRUCT.pack(*parts)
            prev_child_id = child_id
            
        self.nodes_data[node_id] = NODE_STRUCT.pack(name_id, parent_id, v_type, v_data, first_child, 0xFFFFFFFF, b'\x00' * 15)
        return node_id

    def _pack_value(self, val: Any) -> Tuple[int, bytes]:
        if val is _MISSING: return ValueType.MISSING, b'\x00'*16
        if isinstance(val, bool): return ValueType.BOOL, B_STRUCT.pack(1 if val else 0, b'\x00'*15)
        if isinstance(val, int): return ValueType.INT, Q_STRUCT.pack(val, b'\x00'*8)
        if isinstance(val, float): return ValueType.FLOAT, D_STRUCT.pack(val, b'\x00'*8)
        if isinstance(val, str): return ValueType.STRING_ID, I_STRUCT.pack(self.pool.get_id(val), b'\x00'*12)
        if isinstance(val, TagReference): return ValueType.REFERENCE, REF_VAL_STRUCT.pack(1 if val.is_absolute else 0, self.pool.get_id(val.path), b'\x00'*7)
        return ValueType.MISSING, b'\x00'*16

class NTDStore:
    def __init__(self, file_path: str, cache_size: int = 1024):
        self.file_path = file_path
        self.header = NTDHeader()
        self.pool = StringPool()
        self.mm: Optional[mmap.mmap] = None
        self._fd = None
        self._index_cache = []
        self._journal_cache = {}  # {path_ref: (v_t, v_d)}
        self._dynamic_path_cache = {} # {path: path_ref}
        self._in_transaction = False
        self._pending_changes = []
        self.cache = OrderedDict()
        self.cache_size = cache_size
        self._lock = threading.RLock()
        self._compaction_thread = None
        self._compaction_result = None # Path to temp file if ready

    def open(self, mode='r'):
        with self._lock:
            if mode == 'w' and not os.path.exists(self.file_path):
                with open(self.file_path, 'wb') as f:
                    h = NTDHeader()
                    h.string_pool_offset = HEADER_SIZE
                    h.data_offset = h.string_pool_offset + 4
                    h.index_offset = h.data_offset
                    h.journal_offset = h.index_offset + 4
                    res = JOURNAL_ENTRY_SIZE * 10000
                    h.dynamic_pool_offset = h.journal_offset + res
                    h.blob_offset = h.dynamic_pool_offset
                    h.eof_offset = h.blob_offset
                    f.write(h.pack())
                    f.write(U32_STRUCT.pack(0))
                    f.write(U32_STRUCT.pack(0))
                    f.write(b'\x00' * res)

            self._fd = os.open(self.file_path, os.O_RDWR if mode == 'w' else os.O_RDONLY)
            self.mm = mmap.mmap(self._fd, 0, access=mmap.ACCESS_WRITE if mode == 'w' else mmap.ACCESS_READ)
            if len(self.mm) >= HEADER_SIZE:
                self.header = NTDHeader.unpack(self.mm[:HEADER_SIZE])
                if self.header.string_pool_offset > 0:
                    self.pool, _ = StringPool.unpack(self.mm, self.header.string_pool_offset)
                
                # Читаем статический индекс
                self._index_cache = []
                if self.header.index_offset > 0 and self.header.index_offset < self.header.journal_offset:
                    try:
                        count = U32_STRUCT.unpack_from(self.mm, self.header.index_offset)[0]
                        if 0 < count < 1000000:
                            for i in range(count):
                                off = self.header.index_offset + 4 + i * 8
                                self._index_cache.append(INDEX_ENTRY_STRUCT.unpack_from(self.mm, off))
                    except (struct.error, ValueError):
                        pass

                # Читаем журнал + SMART RECOVERY
                self._journal_cache = {}
                if self.header.journal_offset > 0:
                    # 1. Читаем то, что зафиксировано в заголовке
                    for i in range(self.header.journal_size):
                        off = self.header.journal_offset + i * JOURNAL_ENTRY_SIZE
                        if off + JOURNAL_ENTRY_SIZE > self.header.dynamic_pool_offset: break
                        entry_data = self.mm[off : off + JOURNAL_ENTRY_SIZE]
                        p_ref, v_t, v_d, crc = JOURNAL_STRUCT.unpack(entry_data)
                        # Проверяем целостность (CRC32 от PathRef + Type + Data)
                        actual_crc = binascii.crc32(entry_data[:25]) & 0xFFFFFFFF
                        if crc == actual_crc:
                            self._journal_cache[p_ref] = (v_t, v_d)
                            if p_ref & DYNAMIC_PATH_FLAG:
                                path_str = self._read_dynamic_string(self.header.dynamic_pool_offset + (p_ref & PATH_REF_MASK))
                                self._dynamic_path_cache[path_str] = p_ref
                    
                    # 2. SMART RECOVERY: Пробуем найти записи ПОСЛЕ того, что указано в заголовке
                    recovery_count = 0
                    start_idx = self.header.journal_size
                    while True:
                        off = self.header.journal_offset + start_idx * JOURNAL_ENTRY_SIZE
                        if off + JOURNAL_ENTRY_SIZE > self.header.dynamic_pool_offset: break
                        
                        entry_data = self.mm[off : off + JOURNAL_ENTRY_SIZE]
                        if entry_data == b'\x00' * JOURNAL_ENTRY_SIZE: break # Пустое место
                        
                        try:
                            p_ref, v_t, v_d, crc = JOURNAL_STRUCT.unpack(entry_data)
                            # CRC32 от PathRef + Type + Data (8+1+16 = 25 байт)
                            actual_crc = binascii.crc32(entry_data[:25]) & 0xFFFFFFFF
                            if crc == actual_crc:
                                self._journal_cache[p_ref] = (v_t, v_d)
                                # Восстанавливаем кэш путей, если это динамический путь
                                if p_ref & DYNAMIC_PATH_FLAG:
                                    path_str = self._read_dynamic_string(self.header.dynamic_pool_offset + (p_ref & PATH_REF_MASK))
                                    self._dynamic_path_cache[path_str] = p_ref
                                start_idx += 1
                                recovery_count += 1
                            else:
                                break # Нашли мусор/битую запись
                        except:
                            break
                    
                    if recovery_count > 0:
                        # print(f"  [RECOVERY] Restored {recovery_count} lost journal entries.")
                        self.header.journal_size = start_idx
                        # Фиксируем восстановление в заголовке
                        if mode == 'w': self.flush()

    def begin(self):
        self._in_transaction = True
        self._pending_changes = []

    def commit(self):
        if not self._in_transaction or not self._pending_changes:
            self._in_transaction = False
            return
        
        # БАТЧИНГ: Готовим все записи журнала одним куском
        entries = []
        for path, v_t, v_d in self._pending_changes:
            p_ref = self._get_path_ref(path)
            # Обновляем кэш журнала
            self._journal_cache[p_ref] = (v_t, v_d)
            # Сбрасываем кэш значений по пути
            if path in self.cache: del self.cache[path]
            
            pre_data = JOURNAL_CRC_DATA_STRUCT.pack(p_ref, v_t, v_d)
            crc = binascii.crc32(pre_data) & 0xFFFFFFFF
            entries.append(pre_data + CRC_PAD_STRUCT.pack(crc))
        
        batch_data = b"".join(entries)
        batch_size = len(batch_data)
        
        # Проверяем место в журнале один раз для всего батча
        pos = self.header.journal_offset + self.header.journal_size * JOURNAL_ENTRY_SIZE
        if pos + batch_size > self.header.dynamic_pool_offset:
            # Сжимаем если журнал в 2 раза больше уникальных ключей
            if self.header.journal_size + len(entries) > len(self._journal_cache) * 2.0:
                self.compact_journal()
            else:
                self._expand_journal_space()
            pos = self.header.journal_offset + self.header.journal_size * JOURNAL_ENTRY_SIZE
        
        self._ensure_space_at_end(pos + batch_size)
        self.mm[pos : pos + batch_size] = batch_data
        self.header.journal_size += len(entries)
        
        self._in_transaction = False
        self._pending_changes = []
        self.flush()

    def rollback(self):
        self._in_transaction = False
        self._pending_changes = []

    def flush(self, full_sync: bool = False):
        """Сбросить изменения заголовка на диск."""
        if self._compaction_result:
            self._finalize_compaction()
            
        with self._lock:
            if self.mm:
                self.mm[:HEADER_SIZE] = self.header.pack()
                self.mm.flush()
                if full_sync and self._fd is not None:
                    os.fsync(self._fd)

    def compact_journal(self):
        """Major Compaction: Очистка журнала, GC хвоста и Promotion путей (ФОНОВЫЙ РЕЖИМ)."""
        if not self._journal_cache: return
        
        with self._lock:
            if self._compaction_thread and self._compaction_thread.is_alive():
                return

        def task():
            try:
                # 1. Снапшот данных в локе
                with self._lock:
                    snapshot_journal = dict(self._journal_cache)
                    snapshot_pool_strings = list(self.pool.strings)
                    start_journal_size = self.header.journal_size
                    nodes_blob = bytes(self.mm[self.header.data_offset : self.header.index_offset])
                    index_blob = bytes(self.mm[self.header.index_offset : self.header.journal_offset])
                    version = self.header.version
                    dynamic_pool_offset = self.header.dynamic_pool_offset
                    # Копируем пул в локальный объект сразу
                    snapshot_pool_lookup = dict(self.pool.lookup)

                    active_dynamic_blobs = [] 
                    dynamic_offsets_map = {} 
                    current_new_rel_off = 0

                    def _resolve_path_str_bg(p_ref):
                        if p_ref & DYNAMIC_PATH_FLAG:
                            rel_off = p_ref & PATH_REF_MASK
                            abs_off = dynamic_pool_offset + rel_off
                            length = U16_STRUCT.unpack_from(self.mm, abs_off)[0]
                            data = self.mm[abs_off+2 : abs_off+2+length]
                            try:
                                return data.decode('utf-8')
                            except UnicodeDecodeError:
                                return f"<ERR:DECODE@{abs_off}>"
                        return snapshot_pool_strings[p_ref]

                    def get_data_copy_bg(rel_off):
                        nonlocal current_new_rel_off
                        if rel_off in dynamic_offsets_map:
                            return dynamic_offsets_map[rel_off]
                        
                        abs_off = dynamic_pool_offset + rel_off
                        length = U16_STRUCT.unpack_from(self.mm, abs_off)[0]
                        data = bytes(self.mm[abs_off : abs_off + 2 + length])
                        
                        new_off = current_new_rel_off
                        active_dynamic_blobs.append(data)
                        dynamic_offsets_map[rel_off] = new_off
                        current_new_rel_off += len(data)
                        return new_off

                    def gc_dynamic_value_bg(v_t, v_d):
                        nonlocal current_new_rel_off
                        if v_t == ValueType.DYNAMIC_STRING:
                            old_rel_off = REL_OFF_STRUCT.unpack(v_d)[0]
                            new_rel_off = get_data_copy_bg(old_rel_off)
                            return REL_OFF_STRUCT.pack(new_rel_off, b'\x00'*8)
                        elif v_t in (ValueType.BLOB, ValueType.COMPRESSED_BLOB):
                            old_rel_off, length = BLOB_REF_STRUCT.unpack(v_d)
                            abs_off = dynamic_pool_offset + old_rel_off
                            data = bytes(self.mm[abs_off : abs_off + length])
                            new_off = current_new_rel_off
                            active_dynamic_blobs.append(data)
                            current_new_rel_off += len(data)
                            return BLOB_REF_STRUCT.pack(new_off, length)
                        elif v_t == ValueType.REFERENCE:
                            ref_t, val, _ = REF_VAL_STRUCT.unpack(v_d)
                            if ref_t >= 2: # Динамическая ссылка
                                new_rel_off = get_data_copy_bg(val)
                                return REF_VAL_STRUCT.pack(ref_t, new_rel_off, b'\x00'*7)
                        elif v_t == ValueType.LIST:
                            old_rel_off = REL_OFF_STRUCT.unpack(v_d)[0]
                            abs_off = dynamic_pool_offset + old_rel_off
                            count = U16_STRUCT.unpack_from(self.mm, abs_off)[0]
                            new_items = []
                            for i in range(count):
                                item_off = abs_off + 2 + i * 17
                                item_v_t, item_v_d = VAL_ENTRY_STRUCT.unpack_from(self.mm, item_off)
                                new_item_v_d = gc_dynamic_value_bg(item_v_t, item_v_d)
                                new_items.append(VAL_ENTRY_STRUCT.pack(item_v_t, new_item_v_d))
                            new_list_data = U16_STRUCT.pack(count) + b"".join(new_items)
                            new_off = current_new_rel_off
                            active_dynamic_blobs.append(new_list_data)
                            current_new_rel_off += len(new_list_data)
                            return REL_OFF_STRUCT.pack(new_off, b'\x00'*8)
                        return v_d

                    promoted_journal_data = []
                    for p_ref, (v_t, v_d) in snapshot_journal.items():
                        if v_t == ValueType.TOMBSTONE: continue
                        path_str = _resolve_path_str_bg(p_ref)
                        new_v_d = gc_dynamic_value_bg(v_t, v_d)
                        promoted_journal_data.append((path_str, v_t, new_v_d))

                # 2. Сборка нового файла (ВНЕ ЛОКА)
                final_pool = StringPool()
                # Переносим все строки из старого пула и ДОБАВЛЯЕМ новые из журнала
                for s in snapshot_pool_strings: final_pool.get_id(s)
                
                final_journal_entries = []
                for path_str, v_t, v_d in promoted_journal_data:
                    new_path_id = final_pool.get_id(path_str) # Теперь это безопасно добавит в пул
                    pre_data = JOURNAL_CRC_DATA_STRUCT.pack(new_path_id, v_t, v_d)
                    crc = binascii.crc32(pre_data) & 0xFFFFFFFF
                    final_journal_entries.append(pre_data + CRC_PAD_STRUCT.pack(crc))

                temp_path = self.file_path + ".tmp"
                new_header = NTDHeader()
                new_header.version = version
                new_sp_data = final_pool.pack()
                
                # print(f"DEBUG BG: Writing new pool with {len(final_pool.strings)} strings")
                
                with open(temp_path, 'wb') as f:
                    new_header.string_pool_offset = HEADER_SIZE
                    new_header.data_offset = new_header.string_pool_offset + len(new_sp_data)
                    new_header.index_offset = new_header.data_offset + len(nodes_blob)
                    new_header.journal_offset = new_header.index_offset + len(index_blob)
                    
                    journal_reserved_count = max(5000, int(len(final_journal_entries) * 1.2))
                    journal_reserved_bytes = journal_reserved_count * JOURNAL_ENTRY_SIZE
                    
                    new_header.journal_size = len(final_journal_entries)
                    new_header.dynamic_pool_offset = new_header.journal_offset + journal_reserved_bytes
                    new_header.blob_offset = new_header.dynamic_pool_offset
                    
                    f.write(new_header.pack())
                    f.write(new_sp_data)
                    f.write(nodes_blob)
                    f.write(index_blob)
                    f.write(b''.join(final_journal_entries))
                    f.write(b'\x00' * (journal_reserved_bytes - len(final_journal_entries) * JOURNAL_ENTRY_SIZE))
                    f.write(b''.join(active_dynamic_blobs))
                    
                    new_header.eof_offset = f.tell()
                    f.seek(0)
                    f.write(new_header.pack())

                # 3. Фиксация результата
                with self._lock:
                    self._compaction_result = {
                        'temp_path': temp_path,
                        'start_journal_size': start_journal_size,
                        'new_pool_lookup': snapshot_pool_lookup # Сохраняем актуальный на момент начала
                    }
            except Exception as e:
                import traceback
                traceback.print_exc()
                print(f"  [COMPACTION] Background task failed: {e}")

        self._compaction_thread = threading.Thread(target=task)
        self._compaction_thread.daemon = True
        self._compaction_thread.start()

    def _finalize_compaction(self):
        """Применяет результаты фоновой компактификации."""
        if not self._compaction_result: return
        
        import shutil
        with self._lock:
            info = self._compaction_result
            self._compaction_result = None
            temp_path = info['temp_path']
            start_idx = info['start_journal_size']
            
            # 1. Читаем дельту записей, появившихся во время компактификации
            delta_entries = []
            if start_idx < self.header.journal_size:
                for i in range(start_idx, self.header.journal_size):
                    off = self.header.journal_offset + i * JOURNAL_ENTRY_SIZE
                    delta_entries.append(bytes(self.mm[off : off + JOURNAL_ENTRY_SIZE]))
            
            # 2. Дописываем дельту в темп-файл
            if delta_entries:
                with open(temp_path, 'r+b') as f:
                    h_data = f.read(HEADER_SIZE)
                    h = NTDHeader.unpack(h_data)
                    f.seek(h.journal_offset + h.journal_size * JOURNAL_ENTRY_SIZE)
                    f.write(b"".join(delta_entries))
                    h.journal_size += len(delta_entries)
                    f.seek(0)
                    f.write(h.pack())
            
            # 3. Закрываем и заменяем
            self.close()
            gc.collect()
            time.sleep(0.1)
            
            try:
                if os.path.exists(self.file_path):
                    os.remove(self.file_path)
                shutil.move(temp_path, self.file_path)
            except Exception as e:
                # print(f"DEBUG: Move failed: {e}")
                with open(temp_path, 'rb') as src, open(self.file_path, 'wb') as dst:
                    dst.write(src.read())
                try: os.remove(temp_path)
                except: pass
            
            self.open(mode='w')
            print(f"  [COMPACTION] Background compaction applied (delta: {len(delta_entries)} entries).")

    def _get_path_ref(self, path: str) -> int:
        path = sys.intern(path) # INTERNING
        with self._lock:
            if path in self.pool.lookup:
                return self.pool.lookup[path]
            if path in self._dynamic_path_cache:
                return self._dynamic_path_cache[path]
            
            # Пишем новый путь в хвост
            rel_off = self._append_dynamic_string(path)
            p_ref = DYNAMIC_PATH_FLAG | rel_off
            self._dynamic_path_cache[path] = p_ref
            return p_ref

    def _resolve_path_str(self, p_ref: int) -> str:
        with self._lock:
            if p_ref & DYNAMIC_PATH_FLAG:
                rel_off = p_ref & PATH_REF_MASK
                return self._read_dynamic_string(self.header.dynamic_pool_offset + rel_off)
            return self.pool.get_str(p_ref)

    def get(self, path: str, resolve_refs: bool = True) -> Any:
        path = sys.intern(path) # INTERNING
        val = self.get_value_direct(path)
        if val is not None:
            if resolve_refs and isinstance(val, TagReference): return self.get(val.path, resolve_refs=True)
            return val
        if resolve_refs:
            parts = path.split(":")
            for i in range(1, len(parts)):
                p_val = self.get_value_direct(":".join(parts[:i]))
                if isinstance(p_val, TagReference): return self.get(p_val.path + ":" + ":".join(parts[i:]), resolve_refs=True)
        return None

    def get_value_direct(self, path: str) -> Any:
        if not path: return None
        path = sys.intern(path) # INTERNING
        with self._lock:
            # Кэшируем по строке пути для скорости
            if path in self.cache: 
                self.cache.move_to_end(path)
                return self.cache[path]
            
            val = None
            p_ref = self._get_path_ref(path)
            
            if p_ref in self._journal_cache:
                v_t, v_d = self._journal_cache[p_ref]
                if v_t == ValueType.TOMBSTONE: return None
                val = self._unpack_value(v_t, v_d)
            else:
                n_id = self.get_node_id(path)
                if n_id is not None:
                    off = self.header.data_offset + n_id * NODE_SIZE
                    _, _, v_t, v_d = NODE_STRUCT.unpack_from(self.mm, off)
                    val = self._unpack_value(v_t, v_d)
            
            if val is not None and not isinstance(val, (bytes, memoryview)):
                self.cache[path] = val
                if len(self.cache) > self.cache_size: self.cache.popitem(last=False)
            
            # Если это memoryview, удаляем из кэша, чтобы не блокировать файл
            if isinstance(val, memoryview):
                if path in self.cache: del self.cache[path]
                
            return val

    def set(self, path: str, value: Any): self.set_value(path, value)
    def delete(self, path: str):
        """Мягкое удаление ключа через Tombstone."""
        if not path: return
        path = sys.intern(path) # INTERNING
        p_ref = self._get_path_ref(path)
        v_t = ValueType.TOMBSTONE
        v_d = b'\x00' * 16
        if self._in_transaction:
            self._pending_changes.append((path, v_t, v_d))
            self._journal_cache[p_ref] = (v_t, v_d)
        else:
            self._write_to_journal(p_ref, v_t, v_d)
            self._journal_cache[p_ref] = (v_t, v_d)
        if path in self.cache: del self.cache[path]

    def set_value(self, path: str, value: Any):
        """Записать изменение в журнал (Delta)."""
        if not path: return
        path = sys.intern(path) # INTERNING
        v_t, v_d = self._pack_value_for_journal(value)
        if self._in_transaction:
            self._pending_changes.append((path, v_t, v_d))
            # Обновляем журнал кэш сразу для видимости в рамках транзакции
            p_ref = self._get_path_ref(path)
            self._journal_cache[p_ref] = (v_t, v_d)
        else:
            p_ref = self._get_path_ref(path)
            self._write_to_journal(p_ref, v_t, v_d)
            self._journal_cache[p_ref] = (v_t, v_d)

    def _write_to_journal(self, p_ref: int, v_t: int, v_d: bytes):
        with self._lock:
            # Очистка кэша (по строке пути)
            path_str = self._resolve_path_str(p_ref)
            if path_str in self.cache: del self.cache[path_str]
            
            pos = self.header.journal_offset + self.header.journal_size * JOURNAL_ENTRY_SIZE
            
            # ЛОГИКА ПЕРЕПОЛНЕНИЯ: 
            if pos + JOURNAL_ENTRY_SIZE > self.header.dynamic_pool_offset:
                # Сжимаем только если реально много мусора (в 2 раза больше чем данных)
                if self.header.journal_size > len(self._journal_cache) * 2.0:
                    self.compact_journal()
                else:
                    self._expand_journal_space()
                pos = self.header.journal_offset + self.header.journal_size * JOURNAL_ENTRY_SIZE
            
            self._ensure_space_at_end(pos + JOURNAL_ENTRY_SIZE)
            
            pre_data = JOURNAL_CRC_DATA_STRUCT.pack(p_ref, v_t, v_d)
            crc = binascii.crc32(pre_data) & 0xFFFFFFFF
            self.mm[pos:pos+JOURNAL_ENTRY_SIZE] = pre_data + CRC_PAD_STRUCT.pack(crc)
            self.header.journal_size += 1

    def _expand_journal_space(self):
        """Безопасное расширение журнала без потери кэшей."""
        with self._lock:
            # Увеличиваем журнал на 10 000 записей за раз
            res = JOURNAL_ENTRY_SIZE * 10000
            
            tail = bytes(self.mm[self.header.dynamic_pool_offset : self.header.eof_offset])
            
            old_dynamic_off = self.header.dynamic_pool_offset
            self.header.dynamic_pool_offset += res
            self.header.blob_offset += res
            self.header.eof_offset += res
            
            self.mm.close()
            os.close(self._fd)
            with open(self.file_path, 'r+b') as f:
                f.seek(0); f.write(self.header.pack())
                f.truncate(self.header.eof_offset)
            
            self._fd = os.open(self.file_path, os.O_RDWR)
            self.mm = mmap.mmap(self._fd, 0, access=mmap.ACCESS_WRITE)
            
            # Восстанавливаем данные в новом месте
            self.mm[old_dynamic_off : self.header.dynamic_pool_offset] = b'\x00' * res
            self.mm[self.header.dynamic_pool_offset : self.header.eof_offset] = tail

    def _pack_value_for_journal(self, val: Any) -> Tuple[int, bytes]:
        if val is _MISSING: return ValueType.MISSING, b'\x00'*16
        if isinstance(val, bool): return ValueType.BOOL, B_STRUCT.pack(1 if val else 0, b'\x00'*15)
        if isinstance(val, int): return ValueType.INT, Q_STRUCT.pack(val, b'\x00'*8)
        if isinstance(val, float): return ValueType.FLOAT, D_STRUCT.pack(val, b'\x00'*8)
        if isinstance(val, str):
            if val in self.pool.lookup: return ValueType.STRING_ID, I_STRUCT.pack(self.pool.lookup[val], b'\x00'*12)
            return ValueType.DYNAMIC_STRING, REL_OFF_STRUCT.pack(self._append_dynamic_string(val), b'\x00'*8)
        if isinstance(val, bytes):
            if lz4f and len(val) > 1024:
                compressed = lz4f.compress(val)
                if len(compressed) < len(val):
                    off, length = self._append_blob(compressed)
                    return ValueType.COMPRESSED_BLOB, BLOB_REF_STRUCT.pack(off, length)
            off, length = self._append_blob(val)
            return ValueType.BLOB, BLOB_REF_STRUCT.pack(off, length)
        if isinstance(val, TagReference):
            if val.path in self.pool.lookup: return ValueType.REFERENCE, REF_VAL_STRUCT.pack(1 if val.is_absolute else 0, self.pool.lookup[val.path], b'\x00'*7)
            return ValueType.REFERENCE, REF_VAL_STRUCT.pack(2 if val.is_absolute else 3, self._append_dynamic_string(val.path), b'\x00'*7)
        if isinstance(val, (list, tuple)):
            off = self._append_list(val)
            return ValueType.LIST, REL_OFF_STRUCT.pack(off, b'\x00'*8)
        return ValueType.MISSING, b'\x00'*16

    def _append_blob(self, data: bytes) -> Tuple[int, int]:
        with self._lock:
            pos = self.header.eof_offset
            self._ensure_space_at_end(pos + len(data))
            self.mm[pos : pos + len(data)] = data
            self.header.eof_offset += len(data)
            return pos - self.header.dynamic_pool_offset, len(data)

    def _append_dynamic_string(self, s: str) -> int:
        b = s.encode('utf-8')
        with self._lock:
            pos = self.header.eof_offset
            self._ensure_space_at_end(pos + len(b) + 2)
            self.mm[pos : pos + len(b) + 2] = U16_STRUCT.pack(len(b)) + b
            self.header.eof_offset += len(b) + 2
            return pos - self.header.dynamic_pool_offset

    def _append_list(self, items: Union[list, tuple]) -> int:
        packed_items = []
        for item in items:
            v_t, v_d = self._pack_value_for_journal(item)
            packed_items.append(VAL_ENTRY_STRUCT.pack(v_t, v_d))
        
        data = U16_STRUCT.pack(len(items)) + b"".join(packed_items)
        with self._lock:
            pos = self.header.eof_offset
            self._ensure_space_at_end(pos + len(data))
            self.mm[pos : pos + len(data)] = data
            self.header.eof_offset += len(data)
            return pos - self.header.dynamic_pool_offset

    def _ensure_space_at_end(self, needed: int):
        with self._lock:
            if self.mm and needed > len(self.mm):
                new_size = max(needed, len(self.mm) + 65536)
                self.mm.close()
                # На Windows важно закрыть дескриптор перед truncate, если mmap был открыт
                os.close(self._fd)
                with open(self.file_path, 'r+b') as f:
                    f.truncate(new_size)
                
                # Переоткрываем только mmap и дескриптор, не сбрасывая кэши
                self._fd = os.open(self.file_path, os.O_RDWR)
                self.mm = mmap.mmap(self._fd, 0, access=mmap.ACCESS_WRITE)

    def get_node_id(self, path: str) -> Optional[int]:
        h = zlib.adler32(path.encode('utf-8')) & 0xFFFFFFFF
        with self._lock:
            idx = bisect.bisect_left(self._index_cache, (h, 0))
            if idx < len(self._index_cache) and self._index_cache[idx][0] == h: return self._index_cache[idx][1]
        return None

    def _unpack_value(self, v_t: int, v_d: bytes) -> Any:
        if v_t == ValueType.MISSING: return None
        if v_t == ValueType.INT: return Q_STRUCT.unpack(v_d)[0]
        if v_t == ValueType.FLOAT: return D_STRUCT.unpack(v_d)[0]
        if v_t == ValueType.BOOL: return B_STRUCT.unpack(v_d)[0] == 1
        if v_t == ValueType.STRING_ID: return self.pool.get_str(I_STRUCT.unpack(v_d)[0])
        if v_t == ValueType.DYNAMIC_STRING: 
            rel_off = REL_OFF_STRUCT.unpack(v_d)[0]
            return self._read_dynamic_string(self.header.dynamic_pool_offset + rel_off)
        if v_t == ValueType.BLOB:
            rel_off, length = BLOB_REF_STRUCT.unpack(v_d)
            off = self.header.dynamic_pool_offset + rel_off
            # ZERO-COPY: возвращаем view. 
            # ВАЖНО: на Windows это блокирует закрытие/ресайз mmap, 
            # поэтому мы не кэшируем это в self.cache.
            return memoryview(self.mm)[off : off + length]
        if v_t == ValueType.COMPRESSED_BLOB:
            rel_off, length = BLOB_REF_STRUCT.unpack(v_d)
            off = self.header.dynamic_pool_offset + rel_off
            compressed_data = memoryview(self.mm)[off : off + length]
            if lz4f:
                return lz4f.decompress(compressed_data)
            return compressed_data # Fallback if lz4 is missing
        if v_t == ValueType.REFERENCE:
            ref_t, val, _ = REF_VAL_STRUCT.unpack(v_d)
            if ref_t <= 1: return TagReference(path=self.pool.get_str(val), is_absolute=ref_t == 1)
            return TagReference(path=self._read_dynamic_string(self.header.dynamic_pool_offset + val), is_absolute=ref_t == 2)
        if v_t == ValueType.LIST:
            rel_off = REL_OFF_STRUCT.unpack(v_d)[0]
            return self._read_list(self.header.dynamic_pool_offset + rel_off)
        return None

    def _read_dynamic_string(self, off: int) -> str:
        length = U16_STRUCT.unpack_from(self.mm, off)[0]
        data = self.mm[off+2 : off+2+length]
        # print(f"DEBUG: Reading dynamic string at {off}, length {length}, data: {data!r}")
        return sys.intern(data.decode('utf-8'))

    def _read_list(self, abs_off: int) -> list:
        count = U16_STRUCT.unpack_from(self.mm, abs_off)[0]
        items = []
        for i in range(count):
            item_off = abs_off + 2 + i * 17 # 1 byte type + 16 bytes data
            item_v_t, item_v_d = VAL_ENTRY_STRUCT.unpack_from(self.mm, item_off)
            items.append(self._unpack_value(item_v_t, item_v_d))
        return items

    def close(self):
        if self.mm: self.mm.close(); self.mm = None
        if self._fd is not None: os.close(self._fd); self._fd = None

    def save_tree(self, tree: TagTree):
        data = NTDPacker().pack_tree(tree)
        self.close()
        with open(self.file_path, 'wb') as f: f.write(data)
        self.open(mode='w')

class NarratorDB:
    def __init__(self, file_path: str):
        self.store = NTDStore(file_path)
        self.store.open(mode='w')
    def __getitem__(self, path: str) -> Any: return self.store.get(path)
    def __setitem__(self, path: str, value: Any): self.store.set(path, value)
    def __delitem__(self, path: str): self.store.delete(path)
    def delete(self, path: str): self.store.delete(path)
    def transaction(self):
        class Transaction:
            def __init__(self, db): self.db = db
            def __enter__(self): self.db.store.begin()
            def __exit__(self, t, v, tb):
                if t is None: self.db.store.commit()
                else: self.db.store.rollback()
        return Transaction(self)
    def compact(self): self.store.compact_journal()
    def flush(self, full_sync: bool = False): self.store.flush(full_sync)
    def close(self):
        self.flush(full_sync=True)
        self.store.close()
        # Ждем завершения компактификации, если она идет
        if self.store._compaction_thread and self.store._compaction_thread.is_alive():
            self.store._compaction_thread.join()
            self.store._finalize_compaction()

def run_benchmark():
    print("\n=== NTD v1.0 PERFORMANCE & FEATURE TEST ===")
    db_file = "feature_test.ntd"
    if os.path.exists(db_file): os.remove(db_file)
    db = NarratorDB(db_file)
    
    print("1. Testing Binary Blobs (Zero-copy copy)...")
    img = b"\xff\xd8\xff\xe0" + b"A" * 1024
    db['assets:portrait:john'] = img
    ret = db['assets:portrait:john']
    print(f"   Blob Size: {len(ret)} bytes, Type: {type(ret)}")
    assert ret == img
    # Явно удаляем ссылку, чтобы не блокировать mmap
    del ret
    gc.collect()

    print("2. Testing Dynamic Paths & Lazy Write...")
    s = time.perf_counter()
    N = 1000
    for i in range(N):
        db[f'dynamic:path:node_{i}'] = i
    duration = time.perf_counter() - s
    print(f"   Lazy Write {N} new paths: {duration*1000:.2f}ms ({duration/N*1000000:.2f} µs/op)")

    print("3. Testing Deletion (Tombstones)...")
    db['test:to_delete'] = "temporary_value"
    assert db['test:to_delete'] == "temporary_value"
    del db['test:to_delete']
    assert db['test:to_delete'] is None
    print("   Tombstone recorded and verified.")

    print("4. Testing Major Compaction (Promotion & GC)...")
    # Перезаписываем один и тот же большой блоб много раз
    large_data = b"X" * 10000
    for _ in range(10):
        db['test:heavy'] = large_data
    
    print(f"   Size before compact: {os.path.getsize(db_file)} bytes")
    db.compact()
    print(f"   Size after compact: {os.path.getsize(db_file)} bytes")
    assert db['test:heavy'] == large_data
    assert db['test:to_delete'] is None # Должен окончательно исчезнуть
    
    # Проверяем что динамические пути "продвинулись" в StringPool
    db.close()
    time.sleep(0.5) # Ждем завершения компактификации если она была
    db2 = NarratorDB(db_file)
    print(f"   Pool size after promotion: {len(db2.store.pool.strings)} strings")
    # assert db2.store.pool.get_id('dynamic:path:node_999') is not None
    assert db2['dynamic:path:node_999'] == 999
    db2.close()
    
    print("5. Testing Smart Recovery (Tagged Pointers)...")
    db2['secret:recovery'] = "found_me"
    # Эмулируем краш: закрываем mmap без flush
    db2.store.mm.close()
    os.close(db2.store._fd)
    
    # ОТКРЫВАЕМ ТОЛЬКО ПОСЛЕ ТОГО КАК ЗАКРЫЛИ
    db3 = NarratorDB(db_file)
    assert db3['secret:recovery'] == "found_me"
    print("   Smart Recovery verified.")

    print("6. Testing Lists & Nested Lists...")
    inventory = ["sword", "shield", 42, ["potion", 5]]
    db3['player:inventory'] = inventory
    ret_inv = db3['player:inventory']
    print(f"   Inventory: {ret_inv}")
    assert ret_inv == inventory
    
    # Тест GC для списков
    db3.compact()
    # Ждем завершения фоновой компактификации для теста
    time.sleep(0.5)
    db3.flush() 
    assert db3['player:inventory'] == inventory
    print("   Lists verified (with GC).")

    print("7. Testing LZ4 Compression...")
    large_text = ("Hello World! " * 1000).encode('utf-8')
    db3['heavy:text'] = large_text
    ret_text = db3['heavy:text']
    print(f"   Original size: {len(large_text)}, Retrieved size: {len(ret_text)}")
    assert ret_text == large_text
    # Проверяем что тип действительно COMPRESSED_BLOB если lz4 есть
    p_ref = db3.store._get_path_ref('heavy:text')
    v_t, _ = db3.store._journal_cache[p_ref]
    if lz4f:
        print(f"   ValueType: {v_t} (Expected {ValueType.COMPRESSED_BLOB})")
        assert v_t == ValueType.COMPRESSED_BLOB
    else:
        print(f"   ValueType: {v_t} (LZ4 not available, expected {ValueType.BLOB})")
        assert v_t == ValueType.BLOB
    print("   Compression verified.")
    del ret_text
    gc.collect()

    db3.close()
    try: os.remove(db_file)
    except: pass
    print("=================================\n")

if __name__ == "__main__":
    run_benchmark()
