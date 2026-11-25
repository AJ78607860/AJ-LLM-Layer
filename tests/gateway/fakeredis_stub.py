"""Tiny in-memory stand-in for the few redis-py calls RedisStore uses (no server needed)."""


class FakeRedis:
    def __init__(self):
        self.data = {}

    def get(self, key):
        value = self.data.get(key)
        return None if value is None else str(value).encode()

    def set(self, key, value, ex=None):
        self.data[key] = value
        return True

    def delete(self, key):
        return 1 if self.data.pop(key, None) is not None else 0

    def incrbyfloat(self, key, amount):
        self.data[key] = float(self.data.get(key, 0.0)) + float(amount)
        return self.data[key]

    def expire(self, key, seconds):
        return True
