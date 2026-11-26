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

    def hincrbyfloat(self, key, field, amount):
        h = self.data.setdefault(key, {})
        h[field] = float(h.get(field, 0.0)) + float(amount)
        return h[field]

    def hgetall(self, key):
        return {k.encode(): str(v).encode() for k, v in self.data.get(key, {}).items()}

    def sadd(self, key, member):
        self.data.setdefault(key, set()).add(member)

    def smembers(self, key):
        return {m.encode() for m in self.data.get(key, set())}

    def lpush(self, key, value):
        self.data.setdefault(key, []).insert(0, value)

    def ltrim(self, key, start, stop):
        self.data[key] = self.data.get(key, [])[start:stop + 1]

    def lrange(self, key, start, stop):
        items = self.data.get(key, [])
        return [str(v).encode() for v in (items[start:] if stop == -1 else items[start:stop + 1])]
