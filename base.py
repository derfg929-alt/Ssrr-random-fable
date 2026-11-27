import json
import logging
import os
import sqlite3
import threading
from datetime import datetime

import config
import models

log = logging.getLogger("bot.storage")


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
    """Хранилище на JSON-снимке и журнале операций.

    Снимок (config.db_path) перезаписывается целиком только при компакции, а
    каждая мутация дописывается строкой в журнал (<path>.journal). Поэтому
    запись стоит O(1) и не блокирует бота на больших объёмах, а при падении
    процесса данные не теряются: журнал проигрывается при старте.
    """

    def __init__(self, path=None):
        self.path = path or config.db_path
        self.journal = self.path + ".journal"
        self._lock = threading.RLock()
        self._data = {m.__tablename__: [] for m in models.ALL_MODELS}
        self._seq = {m.__tablename__: 0 for m in models.ALL_MODELS}
        self._pk = {m.__tablename__: m.__pk__ for m in models.ALL_MODELS}
        self._journal_ops = 0
        fresh = not os.path.exists(self.path)
        self._load()
        if fresh and self._migrate_legacy_sqlite():
            self._compact()

    # ---- файл ----
    def _load(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        raw = {}
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    raw = json.load(f)
            except Exception:
                backup = f"{self.path}.corrupt-{datetime.now():%Y%m%d-%H%M%S}"
                try:
                    os.replace(self.path, backup)
                    log.error("повреждённый JSON сохранён как %s", backup)
                except Exception:
                    log.exception("не удалось сохранить повреждённый JSON")
                raw = {}
        if not isinstance(raw, dict):
            raw = {}
        for model in models.ALL_MODELS:
            rows = raw.get(model.__tablename__)
            if isinstance(rows, list):
                self._data[model.__tablename__] = rows
        seq = raw.get("__seq__")
        if isinstance(seq, dict):
            for table, value in seq.items():
                if table in self._seq and isinstance(value, int):
                    self._seq[table] = value
        self._sync_seq()
        self._journal_ops = self._replay()
        self._sync_seq()

    def _sync_seq(self):
        """Счётчик id не отстаёт от данных и не переиспользует id удалённых строк."""
        for model in models.ALL_MODELS:
            if not model.__auto__:
                continue
            table = self._data[model.__tablename__]
            current = max((row.get(model.__auto__) or 0 for row in table), default=0)
            self._seq[model.__tablename__] = max(self._seq.get(model.__tablename__, 0), current)

    def _replay(self):
        if not os.path.exists(self.journal):
            return 0
        applied = 0
        try:
            with open(self.journal, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        op = json.loads(line)
                    except ValueError:
                        continue  # обрыв записи при падении процесса
                    if self._apply(op):
                        applied += 1
        except OSError:
            log.exception("не удалось прочитать журнал %s", self.journal)
            return applied
        if applied:
            log.info("применено %d операций из журнала", applied)
        return applied

    def _apply(self, op):
        kind = op.get("o")
        table = op.get("t")
        if table not in self._data:
            return False
        if kind == "n":
            row = op.get("r") or {}
            pk = self._pk.get(table) or ()
            # Компакция и обрезка журнала не атомарны: повтор уже применённой
            # вставки отсекаем по первичному ключу.
            if pk and any(all(r.get(k) == row.get(k) for k in pk) for r in self._data[table]):
                return False
            self._data[table].append(row)
            return True
        if kind == "u":
            values = op.get("v") or {}
            filters = op.get("f") or {}
            for row in self._data[table]:
                if _matches(row, filters):
                    row.update(values)
            return True
        if kind == "d":
            filters = op.get("f") or {}
            self._data[table] = [r for r in self._data[table] if not _matches(r, filters)]
            return True
        return False

    def _append(self, op):
        try:
            with open(self.journal, "a", encoding="utf-8") as f:
                f.write(json.dumps(op, ensure_ascii=False) + "\n")
                if config.DB_FSYNC:
                    f.flush()
                    os.fsync(f.fileno())
        except OSError:
            log.exception("не удалось записать журнал, сохраняю снимок целиком")
            self._compact()
            return
        self._journal_ops += 1
        if self._journal_ops >= config.JOURNAL_COMPACT_OPS:
            self._compact()

    def _compact(self):
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        snapshot = dict(self._data)
        snapshot["__seq__"] = dict(self._seq)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=1)
            if config.DB_FSYNC:
                f.flush()
                os.fsync(f.fileno())
        os.replace(tmp, self.path)
        try:
            open(self.journal, "w", encoding="utf-8").close()
        except OSError:
            log.exception("не удалось очистить журнал %s", self.journal)
        self._journal_ops = 0

    def save(self):
        """Схлопывает журнал в снимок. Вызывается при остановке и перед бэкапом."""
        with self._lock:
            self._compact()

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
            self._sync_seq()
            log.info("перенесено %d записей из %s в %s", migrated, legacy, self.path)
        return migrated > 0

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

    def new(self, Model, _guard=None, **fields):
        """Создаёт строку. _guard вызывается под тем же локом до вставки,
        поэтому лимиты (например, число участников) проверяются атомарно."""
        with self._lock:
            if _guard is not None and not _guard():
                return None
            table = self._data[Model.__tablename__]
            row = {name: _default(field) for name, field in _fields(Model).items()}
            row.update(fields)
            if Model.__auto__ and row.get(Model.__auto__) is None:
                self._seq[Model.__tablename__] = max(
                    self._seq.get(Model.__tablename__, 0),
                    max((r.get(Model.__auto__) or 0 for r in table), default=0)) + 1
                row[Model.__auto__] = self._seq[Model.__tablename__]
            row = self._serialize(Model, row)
            self._check_unique(Model, row, table)
            table.append(row)
            self._append({"o": "n", "t": Model.__tablename__, "r": row})
            return self._to_obj(Model, row)

    def upsert(self, Model, **fields):
        with self._lock:
            table = self._data[Model.__tablename__]
            for row in table:
                if all(row.get(key) == fields.get(key) for key in Model.__pk__):
                    values = self._serialize(Model, fields)
                    row.update(values)
                    self._append({"o": "u", "t": Model.__tablename__, "v": values,
                                  "f": {key: row.get(key) for key in Model.__pk__}})
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
                self._append({"o": "u", "t": Model.__tablename__, "v": values, "f": filters})
            return changed

    def delete(self, Model, **filters):
        with self._lock:
            table = self._data[Model.__tablename__]
            keep = [row for row in table if not _matches(row, filters)]
            removed = len(table) - len(keep)
            if removed:
                self._data[Model.__tablename__] = keep
                self._append({"o": "d", "t": Model.__tablename__, "f": filters})
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
            self._seq = {table: 0 for table in self._seq}
            self._sync_seq()
            self._compact()

    def base_init(self):
        with self._lock:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            if not os.path.exists(self.path):
                self._compact()
