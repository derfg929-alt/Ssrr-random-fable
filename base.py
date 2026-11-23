import json
import os
import sqlite3
import threading
from datetime import datetime

import config
import models


class IntegrityError(Exception):
    """Нарушение уникальности (совместимо с прежним sqlalchemy.exc.IntegrityError)."""


def _fields(cls):
    result = {}
    for klass in reversed(cls.__mro__):
        for name, value in vars(klass).items():
            if isinstance(value, models.Field):
                result[name] = value
    return result


def _default(field):
    return field.default() if callable(field.default) else field.default


def _matches(row, filters):
    return all(row.get(key) == value for key, value in filters.items())


def _sort_key(value):
    # None всегда в конце, чтобы не сравнивать разные типы
    return (1, "") if value is None else (0, value)


class DataBase:
    """Простое хранилище на JSON-файле с тем же API, что был у SQLAlchemy-версии.

    Все данные лежат в одном файле (config.db_path) и сохраняются после каждой
    операции записи. Файл пишется атомарно, поэтому перезапуск/обновление бота
    не теряет пользователей, розыгрыши, участников и состояния.
    """

    def __init__(self, path=None):
        self.path = path or config.db_path
        self._lock = threading.RLock()
        self._data = {m.__tablename__: [] for m in models.ALL_MODELS}
        fresh = not os.path.exists(self.path)
        self._load()
        if fresh and self._migrate_legacy_sqlite():
            self._save()

    # ---- файл ----
    def _load(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            backup = f"{self.path}.corrupt-{datetime.now():%Y%m%d-%H%M%S}"
            try:
                os.replace(self.path, backup)
                print(f"[storage] повреждённый JSON сохранён как {backup}")
            except Exception:
                pass
            return
        for model in models.ALL_MODELS:
            rows = raw.get(model.__tablename__)
            if isinstance(rows, list):
                self._data[model.__tablename__] = rows

    def _migrate_legacy_sqlite(self):
        """Однократно переносит данные старой SQLite-базы в JSON, если она найдена."""
        candidates = [os.getenv("LEGACY_DB"),
                      os.path.join(config.BASE_DIR, "randomizer.db"),
                      os.path.join(os.getcwd(), "randomizer.db")]
        legacy = next((p for p in candidates if p and os.path.isfile(p)), None)
        if not legacy:
            return False
        try:
            conn = sqlite3.connect(legacy)
            conn.row_factory = sqlite3.Row
        except sqlite3.Error:
            return False
        migrated = 0
        try:
            for model in models.ALL_MODELS:
                try:
                    rows = conn.execute(f"SELECT * FROM {model.__tablename__}").fetchall()
                except sqlite3.Error:
                    continue
                names = set(_fields(model))
                for row in rows:
                    data = {key: row[key] for key in row.keys() if key in names}
                    for name in model.__datetime__:
                        value = data.get(name)
                        if isinstance(value, str) and value:
                            data[name] = value.replace(" ", "T", 1)
                    self._data[model.__tablename__].append(data)
                    migrated += 1
        finally:
            conn.close()
        if migrated:
            print(f"[storage] перенесено {migrated} записей из {legacy} в {self.path}")
        return migrated > 0

    def _save(self):
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def save(self):
        with self._lock:
            self._save()

    # ---- конвертация ----
    @staticmethod
    def _serialize(model, row):
        out = dict(row)
        for name in model.__datetime__:
            value = out.get(name)
            if isinstance(value, datetime):
                out[name] = value.isoformat()
        return out

    @staticmethod
    def _to_obj(model, row):
        obj = model()
        for name, field in _fields(model).items():
            value = row[name] if name in row else _default(field)
            if name in model.__datetime__ and isinstance(value, str) and value:
                value = datetime.fromisoformat(value)
            setattr(obj, name, value)
        return obj

    @staticmethod
    def _check_unique(model, row, table):
        combos = list(model.__unique__)
        if model.__pk__ and model.__pk__ != (model.__auto__,):
            combos.append(model.__pk__)
        for combo in combos:
            if any(row.get(key) is None for key in combo):
                continue
            if any(all(existing.get(key) == row.get(key) for key in combo) for existing in table):
                raise IntegrityError(f"{model.__tablename__}: duplicate {combo}")

    # ---- API ----
    def select_all(self, Model, order_by=None, **filters):
        with self._lock:
            objs = [self._to_obj(Model, row) for row in self._data[Model.__tablename__]
                    if _matches(row, filters)]
        if order_by is not None:
            if isinstance(order_by, models.OrderKey):
                name, reverse = order_by.field.name, order_by.reverse
            elif isinstance(order_by, models.Field):
                name, reverse = order_by.name, False
            else:
                name, reverse = None, False
            if name:
                objs.sort(key=lambda o: _sort_key(getattr(o, name, None)), reverse=reverse)
        return objs

    def get_one(self, Model, **filters):
        with self._lock:
            for row in self._data[Model.__tablename__]:
                if _matches(row, filters):
                    return self._to_obj(Model, row)
        return None

    def count(self, Model, **filters):
        with self._lock:
            return sum(1 for row in self._data[Model.__tablename__] if _matches(row, filters))

    def new(self, Model, **fields):
        with self._lock:
            table = self._data[Model.__tablename__]
            row = {name: _default(field) for name, field in _fields(Model).items()}
            row.update(fields)
            if Model.__auto__ and row.get(Model.__auto__) is None:
                row[Model.__auto__] = max(
                    (r.get(Model.__auto__) or 0 for r in table), default=0) + 1
            row = self._serialize(Model, row)
            self._check_unique(Model, row, table)
            table.append(row)
            self._save()
            return self._to_obj(Model, row)

    def upsert(self, Model, **fields):
        with self._lock:
            table = self._data[Model.__tablename__]
            for row in table:
                if all(row.get(key) == fields.get(key) for key in Model.__pk__):
                    row.update(self._serialize(Model, fields))
                    self._save()
                    return self._to_obj(Model, row)
            return self.new(Model, **fields)

    def update(self, Model, values, **filters):
        """Возвращает число обновлённых строк (удобно как атомарный «замок»)."""
        with self._lock:
            values = self._serialize(Model, values)
            changed = 0
            for row in self._data[Model.__tablename__]:
                if _matches(row, filters):
                    row.update(values)
                    changed += 1
            if changed:
                self._save()
            return changed

    def delete(self, Model, **filters):
        with self._lock:
            table = self._data[Model.__tablename__]
            keep = [row for row in table if not _matches(row, filters)]
            removed = len(table) - len(keep)
            if removed:
                self._data[Model.__tablename__] = keep
                self._save()
            return removed

    def replace_all(self, raw):
        """Полностью заменяет данные содержимым резервной копии."""
        if not isinstance(raw, dict):
            raise ValueError("backup is not a JSON object")
        new = {m.__tablename__: [] for m in models.ALL_MODELS}
        found = False
        for model in models.ALL_MODELS:
            rows = raw.get(model.__tablename__)
            if isinstance(rows, list):
                new[model.__tablename__] = rows
                found = True
        if not found:
            raise ValueError("backup has no known tables")
        with self._lock:
            self._data = new
            self._save()

    def base_init(self):
        with self._lock:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            if not os.path.exists(self.path):
                self._save()
