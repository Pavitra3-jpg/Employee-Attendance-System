# Employee Attendance & Analytics API

FastAPI and MongoDB implementation for the HROne attendance assignment.

## Local run

1. Install Python 3.11+ and MongoDB 6.0+.
2. Create and activate a virtual environment, then install dependencies:

   ```bash
   python -m venv .venv
   source .venv/bin/activate  # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. Set `MONGO_URI` and `MONGO_DB` in your shell or a local `.env` file. Environment variables take precedence.
4. From the project root, run `python sample_seed.py` to load the provided sample data, then run:

   ```bash
   uvicorn app.main:app --reload --port 8000
   ```

5. Open `http://127.0.0.1:8000/docs` to try the endpoints.

The current build covers health, employee create/list, punch-in/out, and paginated attendance listing. Analytics, regularization, and explain endpoints remain to be implemented.

Do not commit `.env` or database credentials.
