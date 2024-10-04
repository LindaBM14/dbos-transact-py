# Welcome to DBOS!

# This is a sample app built with DBOS and FastAPI.
# It displays greetings to visitors and keeps track of how
# many times visitors have been greeted.

# First, let's do imports, create a FastAPI app, and initialize DBOS.

import os

import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from dbos import DBOS, ConfigFile

from .schema import dbos_hello

config: ConfigFile = {
    "name": "example-app",
    "language": "python",
    "database": {
        "hostname": "localhost",
        "port": 5432,
        "username": "postgres",
        "password": os.environ["PGPASSWORD"],
        "app_db_name": "example_app",
    },
    "runtimeConfig": {
        "start": ["python3 main.py"],
    },
    "telemetry": {},
    "env": {},
}


def init(config: ConfigFile) -> None:
    app_db_name = config["database"]["app_db_name"]

    postgres_db_engine = sa.create_engine(
        sa.URL.create(
            "postgresql+psycopg",
            username=config["database"]["username"],
            password=config["database"]["password"],
            host=config["database"]["hostname"],
            port=config["database"]["port"],
            database="postgres",
        )
    )
    with postgres_db_engine.connect() as conn:
        conn.execution_options(isolation_level="AUTOCOMMIT")
        if not conn.execute(
            sa.text("SELECT 1 FROM pg_database WHERE datname=:db_name"),
            parameters={"db_name": app_db_name},
        ).scalar():
            conn.execute(sa.text(f"CREATE DATABASE {app_db_name}"))
    postgres_db_engine.dispose()

    app_db_engine = sa.create_engine(
        sa.URL.create(
            "postgresql+psycopg",
            username=config["database"]["username"],
            password=config["database"]["password"],
            host=config["database"]["hostname"],
            port=config["database"]["port"],
            database=app_db_name,
        )
    )
    with app_db_engine.connect() as conn:
        conn.execution_options(isolation_level="AUTOCOMMIT")
        conn.execute(
            sa.text(
                f"CREATE TABLE IF NOT EXISTS dbos_hello(greet_count SERIAL NOT NULL, name varchar NOT NULL, PRIMARY KEY(greet_count));"
            )
        )
    app_db_engine.dispose()


init(config)

app = FastAPI()
DBOS(fastapi=app, config=config)


# Next, let's write a function that greets visitors.
# To make it more interesting, we'll keep track of how
# many times visitors have been greeted and store
# the count in the database.

# We implement the database operations using SQLAlchemy
# and serve the function from a FastAPI endpoint.
# We annotate it with @DBOS.transaction() to access
# an automatically-configured database client.


@app.get("/greeting/{name}")
@DBOS.transaction()
async def example_transaction(name: str) -> str:
    query = dbos_hello.insert().values(name=name).returning(dbos_hello.c.greet_count)
    greet_count = DBOS.sql_session.execute(query).scalar_one()
    greeting = f"Greetings, {name}! You have been greeted {greet_count} times."
    DBOS.logger.info(greeting)
    return greeting


# Finally, let's use FastAPI to serve an HTML + CSS readme
# from the root path.


@app.get("/")
def readme() -> HTMLResponse:
    readme = """
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <title>Welcome to DBOS!</title>
            <script src="https://cdn.tailwindcss.com"></script>
        </head>
        <body class="font-sans text-gray-800 p-6 max-w-2xl mx-auto">
            <h1 class="text-xl font-semibold mb-4">Welcome to DBOS!</h1>
            <p class="mb-4">
                Visit the route <code class="bg-gray-100 px-1 rounded">/greeting/{name}</code> to be greeted!<br>
                For example, visit <code class="bg-gray-100 px-1 rounded"><a href="/greeting/dbos" class="text-blue-600 hover:underline">/greeting/dbos</a></code><br>
                The counter increments with each page visit.<br>
            </p>
            <p>
                To learn more about DBOS, check out the <a href="https://docs.dbos.dev" class="text-blue-600 hover:underline">docs</a>.
            </p>
        </body>
        </html>
        """
    return HTMLResponse(readme)


# To deploy this app to DBOS Cloud:
# - "npm i -g @dbos-inc/dbos-cloud@latest" to install the Cloud CLI (requires Node)
# - "dbos-cloud app deploy" to deploy your app
# - Deploy outputs a URL--visit it to see your app!


# To run this app locally:
# - Make sure you have a Postgres database to connect to
# - "dbos migrate" to set up your database tables
# - "dbos start" to start the app
# - Visit localhost:8000 to see your app!
