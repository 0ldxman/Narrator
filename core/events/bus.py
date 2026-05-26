from __future__ import annotations
import re
from enum import Enum, auto
from typing import Any, Dict, List, Callable, Optional, Union


class EventPhase(Enum):
    """Фазы прохождения события."""
    BEFORE = auto()  # Модификация и валидация
    ON = auto()      # Основная логика
    AFTER = auto()   # Реакция и визуальные эффекты


class Event:
    """Объект события, передаваемый слушателям."""
    
    def __init__(self, name: str, payload: Dict[str, Any]):
        self.name = name
        self.data = payload  # Назовем коротко data для удобства
        self.phase = EventPhase.BEFORE
        self._cancelled = False

    def cancel(self):
        """Отменяет выполнение события (пропускает фазу ON)."""
        self._cancelled = True

    def is_cancelled(self) -> bool:
        return self._cancelled

    def __getitem__(self, key: str) -> Any:
        """Сахар: доступ к payload через event['key']"""
        return self.data.get(key)

    def __setitem__(self, key: str, value: Any):
        """Сахар: запись в payload через event['key'] = value"""
        self.data[key] = value

    def __repr__(self):
        return f"Event(name={self.name}, phase={self.phase.name}, cancelled={self._cancelled})"


class EventBus:
    """Шина событий с поддержкой иерархии и стадий."""

    def __init__(self):
        # Структура: { "path_pattern": { phase: [callbacks] } }
        self._listeners: Dict[str, Dict[EventPhase, List[Callable[[Event], None]]]] = {}
        self._pattern_cache: Dict[str, re.Pattern] = {}

    def on(self, pattern: str, phase: EventPhase = EventPhase.ON):
        """
        Декоратор для удобной подписки:
        @bus.on("combat:*", phase=EventPhase.BEFORE)
        def my_handler(ev): ...
        """
        def decorator(callback: Callable[[Event], None]):
            self.subscribe(pattern, callback, phase)
            return callback
        return decorator

    def _get_regex(self, pattern: str) -> re.Pattern:
        """Превращает иерархический паттерн с * в регулярку."""
        if pattern not in self._pattern_cache:
            # Заменяем * на группу символов, кроме разделителя :
            # Но если * стоит в конце, позволяем захватывать всё до конца
            regex_str = pattern.replace("*", r"[^:]+")
            # Добавляем якоря начала и конца строки
            self._pattern_cache[pattern] = re.compile(f"^{regex_str}$")
        return self._pattern_cache[pattern]

    def subscribe(self, pattern: str, callback: Callable[[Event], None], phase: EventPhase = EventPhase.ON):
        """Подписывается на событие по паттерну (например, 'combat:*')."""
        if pattern not in self._listeners:
            self._listeners[pattern] = {p: [] for p in EventPhase}
        
        self._listeners[pattern][phase].append(callback)

    def emit(self, name: str, payload: Dict[str, Any]) -> Event:
        """
        Запускает цепочку обработки события по фазам.
        """
        event = Event(name, payload)
        
        # Проходим по всем фазам по порядку
        for phase in EventPhase:
            event.phase = phase
            
            # Если событие отменено и мы дошли до фазы ON - пропускаем её
            if phase == EventPhase.ON and event.is_cancelled():
                continue
            
            self._dispatch_phase(event)
            
        return event

    def _dispatch_phase(self, event: Event):
        """Вызывает всех слушателей для конкретной фазы текущего события."""
        for pattern, phases in self._listeners.items():
            # Проверяем, подходит ли имя события под паттерн слушателя
            if self._get_regex(pattern).match(event.name):
                for callback in phases[event.phase]:
                    callback(event)