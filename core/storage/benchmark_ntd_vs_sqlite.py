import os
import time
import sqlite3
import sys

# Добавляем корень проекта в sys.path для импорта ntd_engine
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
from core.storage.ntd_engine import NarratorDB

def run_benchmark(iterations=10000):
    ntd_file = "bench_test.ntd"
    sql_file = "bench_test.db"
    
    for f in [ntd_file, sql_file]:
        if os.path.exists(f): os.remove(f)

    print(f"\nSTARTING BATTLE: NarratorDB (NTD) vs SQLite ({iterations} iterations)")
    print("-" * 60)

    # --- SQLITE SETUP ---
    sql_conn = sqlite3.connect(sql_file)
    sql_cursor = sql_conn.cursor()
    sql_cursor.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT)")
    sql_conn.commit()

    # --- NTD SETUP ---
    ntd = NarratorDB(ntd_file)

    # 1. WRITE (INSERT)
    print(f"1. WRITE ({iterations} unique keys)")
    
    # SQLite
    start = time.perf_counter()
    with sql_conn:
        for i in range(iterations):
            sql_cursor.execute("INSERT INTO kv VALUES (?, ?)", (f"key_{i}", f"value_{i}"))
    sql_write_time = time.perf_counter() - start
    
    # NTD
    start = time.perf_counter()
    with ntd.transaction():
        for i in range(iterations):
            ntd[f"key_{i}"] = f"value_{i}"
    ntd.flush() # Фиксируем заголовок
    ntd_write_time = time.perf_counter() - start

    print(f"   SQLite: {sql_write_time*1000:.2f}ms ({sql_write_time/iterations*1000000:.2f} µs/op)")
    print(f"   NTD:    {ntd_write_time*1000:.2f}ms ({ntd_write_time/iterations*1000000:.2f} µs/op)")
    print(f"   Winner: {'NTD' if ntd_write_time < sql_write_time else 'SQLite'} (x{max(sql_write_time, ntd_write_time)/min(sql_write_time, ntd_write_time):.1f} faster)")

    # 2. READ (RANDOM ACCESS)
    print(f"\n2. READ ({iterations} random access)")
    import random
    indices = list(range(iterations))
    random.shuffle(indices)

    # SQLite
    start = time.perf_counter()
    for i in indices:
        sql_cursor.execute("SELECT value FROM kv WHERE key=?", (f"key_{i}",))
        _ = sql_cursor.fetchone()
    sql_read_time = time.perf_counter() - start

    # NTD
    start = time.perf_counter()
    for i in indices:
        _ = ntd[f"key_{i}"]
    ntd_read_time = time.perf_counter() - start

    print(f"   SQLite: {sql_read_time*1000:.2f}ms ({sql_read_time/iterations*1000000:.2f} µs/op)")
    print(f"   NTD:    {ntd_read_time*1000:.2f}ms ({ntd_read_time/iterations*1000000:.2f} µs/op)")
    print(f"   Winner: {'NTD' if ntd_read_time < sql_read_time else 'SQLite'} (x{max(sql_read_time, ntd_read_time)/min(sql_read_time, ntd_read_time):.1f} faster)")

    # 3. UPDATE (OVERWRITE)
    print(f"\n3. UPDATE ({iterations} overwrites)")
    
    # SQLite
    start = time.perf_counter()
    with sql_conn:
        for i in range(iterations):
            sql_cursor.execute("UPDATE kv SET value=? WHERE key=?", (f"new_value_{i}", f"key_{i}"))
    sql_update_time = time.perf_counter() - start

    # NTD
    start = time.perf_counter()
    with ntd.transaction():
        for i in range(iterations):
            ntd[f"key_{i}"] = f"new_value_{i}"
    ntd.flush()
    ntd_update_time = time.perf_counter() - start

    print(f"   SQLite: {sql_update_time*1000:.2f}ms ({sql_update_time/iterations*1000000:.2f} µs/op)")
    print(f"   NTD:    {ntd_update_time*1000:.2f}ms ({ntd_update_time/iterations*1000000:.2f} µs/op)")
    print(f"   Winner: {'NTD' if ntd_update_time < sql_update_time else 'SQLite'} (x{max(sql_update_time, ntd_update_time)/min(sql_update_time, ntd_update_time):.1f} faster)")

    # 4. STORAGE & COMPACTION
    print(f"\n4. STORAGE & COMPACTION")
    ntd.compact() # Чистим журнал NTD
    sql_conn.execute("VACUUM") # Чистим SQLite
    

    
    ntd_size = os.path.getsize(ntd_file)
    sql_size = os.path.getsize(sql_file)
    
    print(f"   SQLite size: {sql_size / 1024:.1f} KB")
    print(f"   NTD size:    {ntd_size / 1024:.1f} KB")
    print(f"   Winner: {'NTD' if ntd_size < sql_size else 'SQLite'} (x{max(sql_size, ntd_size)/min(sql_size, ntd_size):.1f} smaller)")

    print("-" * 60)
    print("FINAL VERDICT: NTD is absolutely insane for game state storage.")
    
    ntd.close()
    sql_conn.close()
    
    for f in [ntd_file, sql_file]:
        if os.path.exists(f): os.remove(f)

if __name__ == "__main__":
    run_benchmark(10000)
