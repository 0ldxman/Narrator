import sqlite3
import json

def run_demo():
    # Создаем базу в памяти для тестов
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()

    # 1. Создаем таблицу с JSON колонкой
    print("--- 1. Создание таблицы ---")
    cursor.execute("CREATE TABLE entities (id TEXT PRIMARY KEY, data JSON)")

    # 2. Подготовим данные (наши TagTree дельты в формате JSON)
    entities = [
        ("orc_1", {"stats": {"hp": 100, "str": 15}, "status": ["poisoned", "angry"]}),
        ("orc_2", {"stats": {"hp": 50, "str": 10}, "status": ["poisoned"]}),
        ("sword_1", {"damage": {"base": "1d8", "fire": 2}, "tags": ["magical", "rare"]}),
        ("hero_1", {"stats": {"hp": 150, "str": 20}, "status": []})
    ]

    # Вставляем данные как обычные строки (SQLite сам поймет, что это JSON)
    for eid, data in entities:
        cursor.execute("INSERT INTO entities (id, data) VALUES (?, ?)", (eid, json.dumps(data)))

    # 3. Простой поиск по значению внутри JSON
    print("\n--- 2. Поиск: Все, у кого HP > 80 ---")
    # Используем ->> для получения значения (как в Postgres)
    query = "SELECT id, data ->> '$.stats.hp' as hp FROM entities WHERE data ->> '$.stats.hp' > 80"
    cursor.execute(query)
    for row in cursor.fetchall():
        print(f"Entity: {row[0]}, HP: {row[1]}")

    # 4. Поиск по вложенности (есть ли тег в списке)
    print("\n--- 3. Поиск: Все, у кого есть статус 'poisoned' ---")
    # json_each разворачивает массив в строки
    query = """
    SELECT DISTINCT entities.id 
    FROM entities, json_each(entities.data, '$.status') 
    WHERE json_each.value = 'poisoned'
    """
    cursor.execute(query)
    for row in cursor.fetchall():
        print(f"Poisoned Entity: {row[0]}")

    # 5. Использование виртуальных колонок и индексов
    print("\n--- 4. Виртуальные колонки и Индексы ---")
    cursor.execute("""
        ALTER TABLE entities ADD COLUMN strength 
        GENERATED ALWAYS AS (data ->> '$.stats.str') VIRTUAL
    """)
    cursor.execute("CREATE INDEX idx_strength ON entities(strength)")
    
    print("Виртуальная колонка 'strength' добавлена и проиндексирована.")
    
    # Теперь ищем по обычной колонке, которая сама берет данные из JSON
    cursor.execute("SELECT id, strength FROM entities WHERE strength >= 15")
    for row in cursor.fetchall():
        print(f"Strong Entity: {row[0]} (Str: {row[1]})")

    conn.close()

if __name__ == "__main__":
    # Проверим версию SQLite
    print(f"SQLite version: {sqlite3.sqlite_version}")
    run_demo()
