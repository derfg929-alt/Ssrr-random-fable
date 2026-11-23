from sqlalchemy.orm import sessionmaker

import models

# Новая короткая сессия на каждую операцию: безопасно для потоков (воркер + обработчики)
Session = sessionmaker(bind=models.engine, expire_on_commit=False)


class DataBase:
    def select_all(self, Model, order_by=None, **filters):
        with Session() as s:
            q = s.query(Model).filter_by(**filters)
            if order_by is not None:
                q = q.order_by(order_by)
            return q.all()

    def get_one(self, Model, **filters):
        with Session() as s:
            return s.query(Model).filter_by(**filters).first()

    def count(self, Model, **filters):
        with Session() as s:
            return s.query(Model).filter_by(**filters).count()

    def new(self, Model, **fields):
        with Session() as s:
            obj = Model(**fields)
            s.add(obj)
            s.commit()
            return obj

    def upsert(self, Model, **fields):
        with Session() as s:
            obj = s.merge(Model(**fields))
            s.commit()
            return obj

    def update(self, Model, values, **filters):
        """Возвращает число обновлённых строк (удобно как атомарный «замок»)."""
        with Session() as s:
            n = s.query(Model).filter_by(**filters).update(values)
            s.commit()
            return n

    def delete(self, Model, **filters):
        with Session() as s:
            n = s.query(Model).filter_by(**filters).delete()
            s.commit()
            return n

    def base_init(self):
        models.Base.metadata.create_all(models.engine)
