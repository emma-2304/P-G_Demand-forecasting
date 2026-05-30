# Demand Forecasting — Case Study

Read [CASE_STUDY.md](CASE_STUDY.md) for the full problem description.

## How to work

**1. Start the server:**

```bash
docker compose up
```
Mac:
```bash
docker-compose up
```

**2. Run the tests** (in another terminal):

```bash
docker compose exec app python test_api.py
```
Mac:
```bash
docker-compose exec app python test_api.py
```

**3. Edit `server.py`, then restart:**

```bash
docker compose restart
```
Mac:
```bash
docker-compose restart
```

Repeat steps 2–3 until all tests pass.
