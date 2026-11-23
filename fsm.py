import json

import models


class FSM:
    """Состояние пользователя хранится в БД (JSON), поэтому переживает перезапуск бота."""

    def __init__(self, base):
        self.base = base

    def set_state(self, user_id, state, **data):
        self.base.upsert(models.UserState, user_id=str(user_id), state=state,
                         data=json.dumps(data, ensure_ascii=False))

    def get_state(self, user_id):
        row = self.base.get_one(models.UserState, user_id=str(user_id))
        if row:
            return row.state, json.loads(row.data or "{}")
        return "idle", {}

    def remove_state(self, user_id):
        self.base.delete(models.UserState, user_id=str(user_id))
