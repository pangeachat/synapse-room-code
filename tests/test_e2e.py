import asyncio
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from typing import IO, Literal

import aiounittest
import psycopg2
import requests
import testing.postgresql
import yaml
from psycopg2.extensions import parse_dsn

from synapse_room_code.constants import (
    ACCESS_CODE_JOIN_RULE_CONTENT_KEY,
    JOIN_RULE_CONTENT_KEY,
    KNOCK_JOIN_RULE_VALUE,
    MEMBERSHIP_CONTENT_KEY,
    MEMBERSHIP_INVITE,
)

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.DEBUG,  # Set the logging level
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",  # Log format
    filename="synapse.log",  # File to log to
    filemode="w",  # Append mode (use 'w' to overwrite each time)
)


class TestE2E(aiounittest.AsyncTestCase):
    async def start_test_synapse(
        self,
        db: Literal["sqlite", "postgresql"] = "sqlite",
        postgresql_url: str | None = None,
    ) -> tuple[str, str, subprocess.Popen[str], threading.Thread, threading.Thread]:
        try:
            synapse_dir = tempfile.mkdtemp()

            # Generate Synapse config with server name 'my.domain.name'
            config_path = os.path.join(synapse_dir, "homeserver.yaml")
            generate_config_cmd = [
                sys.executable,
                "-m",
                "synapse.app.homeserver",
                "--server-name=my.domain.name",
                f"--config-path={config_path}",
                "--report-stats=no",
                "--generate-config",
            ]
            subprocess.check_call(generate_config_cmd)

            # Modify the config to include the module
            with open(config_path, "r") as f:
                config = yaml.safe_load(f)
            log_config_path = config.get("log_config")
            config["modules"] = [
                {"module": "synapse_room_code.SynapseRoomCode", "config": {}}
            ]
            if db == "sqlite":
                if postgresql_url is not None:
                    self.fail(
                        "PostgreSQL URL must not be defined when using SQLite database"
                    )
                config["database"] = {
                    "name": "sqlite3",
                    "args": {"database": "homeserver.db"},
                }
            elif db == "postgresql":
                if postgresql_url is None:
                    self.fail("PostgreSQL URL is required for PostgreSQL database")
                dsn_params = parse_dsn(postgresql_url)
                config["database"] = {
                    "name": "psycopg2",
                    "args": dsn_params,
                }

            with open(config_path, "w") as f:
                yaml.dump(config, f)

            # Modify log config to log to console
            with open(config_path, "r") as f:
                config = yaml.safe_load(f)
            with open(log_config_path, "r") as f:
                log_config = yaml.safe_load(f)
            log_config["root"]["handlers"] = ["console"]
            log_config["root"]["level"] = "DEBUG"
            with open(log_config_path, "w") as f:
                yaml.dump(log_config, f)

            # Run the Synapse server
            run_server_cmd = [
                sys.executable,
                "-m",
                "synapse.app.homeserver",
                "--config-path",
                config_path,
            ]
            server_process = subprocess.Popen(
                run_server_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=synapse_dir,
                text=True,
            )

            # Start threads to read stdout and stderr concurrently
            def read_output(pipe: IO[str] | None):
                if pipe is None:
                    return
                for line in iter(pipe.readline, ""):
                    logger.debug(line)
                pipe.close()

            stdout_thread = threading.Thread(
                target=read_output, args=(server_process.stdout,)
            )
            stderr_thread = threading.Thread(
                target=read_output, args=(server_process.stderr,)
            )
            stdout_thread.start()
            stderr_thread.start()

            # Wait for the server to start by polling the root URL
            server_url = "http://localhost:8008"
            max_wait_time = 10  # Maximum wait time in seconds
            wait_interval = 1  # Interval between checks in seconds
            total_wait_time = 0
            server_ready = False
            while server_ready is False and total_wait_time < max_wait_time:
                try:
                    response = requests.get(server_url)
                    if response.status_code == 200:
                        server_ready = True
                        break
                except requests.exceptions.ConnectionError:
                    print(
                        f"Synapse server not yet up, retrying {total_wait_time}/{max_wait_time}..."
                    )
                finally:
                    await asyncio.sleep(wait_interval)
                    total_wait_time += wait_interval

            if server_ready is False:
                self.fail("Synapse server did not start successfully")
            else:
                print("Synapse server started successfully")

            return (
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            )
        except Exception as e:
            server_process.terminate()
            server_process.wait()
            stdout_thread.join()
            stderr_thread.join()
            shutil.rmtree(synapse_dir)
            raise e

    async def create_private_room(self, access_token: str):
        headers = {"Authorization": f"Bearer {access_token}"}
        # Create a room with user 1
        create_room_url = "http://localhost:8008/_matrix/client/v3/createRoom"
        create_room_data = {"visibility": "private", "preset": "private_chat"}
        response = requests.post(
            create_room_url,
            json=create_room_data,
            headers=headers,
        )
        self.assertEqual(response.status_code, 200)
        room_id = response.json()["room_id"]
        self.assertIsInstance(room_id, str)
        return room_id

    async def set_room_knockable_with_code(
        self,
        room_id: str,
        access_token: str,
        access_code: str | None = None,
    ):
        headers = {"Authorization": f"Bearer {access_token}"}
        set_join_rules_url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/state/m.room.join_rules"
        state_event_content = {
            JOIN_RULE_CONTENT_KEY: KNOCK_JOIN_RULE_VALUE,
            ACCESS_CODE_JOIN_RULE_CONTENT_KEY: access_code,
        }
        response = requests.put(
            set_join_rules_url,
            json=state_event_content,
            headers=headers,
        )
        self.assertEqual(response.status_code, 200)
        event_id = response.json()["event_id"]
        self.assertIsInstance(event_id, str)
        return event_id

    async def register_user(
        self, config_path: str, dir: str, user: str, password: str, admin: bool
    ):
        register_user_cmd = [
            "register_new_matrix_user",
            f"-c={config_path}",
            f"--user={user}",
            f"--password={password}",
        ]
        if admin:
            register_user_cmd.append("--admin")
        subprocess.check_call(register_user_cmd, cwd=dir)

    async def login_user(self, user: str, password: str) -> tuple[str, str]:
        login_url = "http://localhost:8008/_matrix/client/v3/login"
        login_data = {
            "type": "m.login.password",
            "user": user,
            "password": password,
        }
        response = requests.post(login_url, json=login_data)
        self.assertEqual(response.status_code, 200)
        response_json = response.json()
        access_token = response_json["access_token"]
        user_id = response_json["user_id"]
        self.assertIsInstance(access_token, str)
        self.assertIsInstance(user_id, str)
        return (user_id, access_token)

    async def knock_with_code(self, access_code: str, access_token: str):
        knock_with_code_url = "http://localhost:8008/_synapse/client/knock_with_code"
        response = requests.post(
            knock_with_code_url,
            json={"access_code": access_code},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(response.status_code, 200)

    async def knock_with_invalid_code(self, access_token: str):
        knock_with_code_url = "http://localhost:8008/_synapse/client/knock_with_code"
        response = requests.post(
            knock_with_code_url,
            json={"access_code": "invalid"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(response.status_code, 400)

    async def wait_for_room_invitation(
        self, room_id: str, user_id: str, access_token: str
    ) -> bool:
        room_state_url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/state/m.room.member/{user_id}"
        total_wait_time = 0
        max_wait_time = 3
        wait_interval = 1
        received_invitation = False
        while total_wait_time < max_wait_time and not received_invitation:
            response = requests.get(
                room_state_url, headers={"Authorization": f"Bearer {access_token}"}
            )
            if (
                response.status_code == 200
                and response.json().get(MEMBERSHIP_CONTENT_KEY) == MEMBERSHIP_INVITE
            ):
                received_invitation = True
                break

            print(
                f"User 2 has not been invited to the room yet, retrying {total_wait_time}/{max_wait_time}..."
            )
            await asyncio.sleep(wait_interval)
            total_wait_time += wait_interval
        return received_invitation

    async def start_test_postgres(self):
        postgresql = None
        try:
            # Start a temporary PostgreSQL instance
            postgresql = testing.postgresql.Postgresql()
            postgres_url = postgresql.url()

            # Wait until the instance is ready to accept connections
            max_waiting_time = 10  # in seconds
            wait_interval = 1  # in seconds
            total_wait_time = 0
            postgres_is_up = False

            while total_wait_time < max_waiting_time and not postgres_is_up:
                try:
                    conn = psycopg2.connect(postgres_url)
                    conn.close()
                    postgres_is_up = True
                    print("Postgres started successfully")
                    break
                except psycopg2.OperationalError:
                    print(
                        f"Postgres is not yet up, retrying {total_wait_time}/{max_waiting_time}..."
                    )
                    await asyncio.sleep(wait_interval)
                    total_wait_time += wait_interval

            if not postgres_is_up:
                postgresql.stop()
                self.fail("Postgres did not start successfully")

            # Create a new database with LC_COLLATE and LC_CTYPE set to 'C'
            conn = psycopg2.connect(postgres_url)
            conn.autocommit = True
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE DATABASE testdb
                WITH TEMPLATE template0
                LC_COLLATE 'C'
                LC_CTYPE 'C';
            """
            )
            cursor.close()
            conn.close()

            # Update the connection parameters to connect to 'testdb'
            dsn_params = parse_dsn(postgres_url)
            dsn_params["dbname"] = "testdb"
            postgres_url_testdb = psycopg2.extensions.make_dsn(**dsn_params)

            # Confirm the collation
            conn = psycopg2.connect(postgres_url_testdb)
            cursor = conn.cursor()
            cursor.execute("SHOW LC_COLLATE;")
            collation = cursor.fetchone()[0]
            print(f"Current collation: {collation}")
            assert collation == "C", f"Expected collation 'C', got '{collation}'"

            cursor.execute("SHOW LC_CTYPE;")
            ctype = cursor.fetchone()[0]
            print(f"Current character classification: {ctype}")
            assert ctype == "C", f"Expected LC_CTYPE 'C', got '{ctype}'"

            cursor.close()
            conn.close()

            # Return both the process and the connection parameters
            return postgresql, postgres_url_testdb

        except Exception as e:
            # Ensure the instance is stopped if an exception occurs
            if postgresql is not None:
                postgresql.stop()
            raise e

    async def test_e2e_sqlite(self) -> None:
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None
        try:
            # Create a temporary directory for the Synapse server
            access_code = "vldcde1"
            (
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse()
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="test1",
                password="123123123",
                admin=True,
            )
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="test2",
                password="123123123",
                admin=True,
            )

            # Login to obtain access token of both users
            user_1_id, user_1_access_token = await self.login_user(
                user="test1", password="123123123"
            )
            user_2_id, user_2_access_token = await self.login_user(
                user="test2", password="123123123"
            )

            room_id = await self.create_private_room(user_1_access_token)

            await self.set_room_knockable_with_code(
                room_id=room_id,
                access_token=user_1_access_token,
                access_code=access_code,
            )

            # Invoke knock with code endpoint
            await self.knock_with_invalid_code(user_2_access_token)
            await self.knock_with_code(access_code, user_2_access_token)

            # Wait for the invite
            received_invitation = await self.wait_for_room_invitation(
                room_id=room_id,
                user_id=user_2_id,
                access_token=user_1_access_token,
            )
            if not received_invitation:
                self.fail("User 2 was not invited to the room")
            else:
                print("User 2 was invited to the room")
            # Clean up
            if server_process is not None:
                server_process.terminate()
                server_process.wait()
            if stdout_thread is not None:
                stdout_thread.join()
            if stderr_thread is not None:
                stderr_thread.join()
            if synapse_dir is not None:
                shutil.rmtree(synapse_dir)
        except Exception as e:
            if server_process is not None:
                server_process.terminate()
                server_process.wait()
            if stdout_thread is not None:
                stdout_thread.join()
            if stderr_thread is not None:
                stderr_thread.join()
            if synapse_dir is not None:
                shutil.rmtree(synapse_dir)
            raise e

    async def test_e2e_postgresql(self) -> None:
        postgres = None
        server_process = None
        server_process = None
        stdout_thread = None
        stderr_thread = None
        synapse_dir = None
        try:
            # Create a temporary directory for the Synapse server
            access_code = "vldcde1"
            postgres, postgres_url = await self.start_test_postgres()
            print(postgres_url)
            (
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse(
                db="postgresql", postgresql_url=postgres_url
            )
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="test1",
                password="123123123",
                admin=True,
            )
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="test2",
                password="123123123",
                admin=True,
            )

            # Login to obtain access token of both users
            user_1_id, user_1_access_token = await self.login_user(
                user="test1", password="123123123"
            )
            user_2_id, user_2_access_token = await self.login_user(
                user="test2", password="123123123"
            )

            room_id = await self.create_private_room(user_1_access_token)

            await self.set_room_knockable_with_code(
                room_id=room_id,
                access_token=user_1_access_token,
                access_code=access_code,
            )

            # Invoke knock with code endpoint
            await self.knock_with_invalid_code(user_2_access_token)
            await self.knock_with_code(access_code, user_2_access_token)

            # Wait for the invite
            received_invitation = await self.wait_for_room_invitation(
                room_id=room_id,
                user_id=user_2_id,
                access_token=user_1_access_token,
            )
            if not received_invitation:
                self.fail("User 2 was not invited to the room")
            else:
                print("User 2 was invited to the room")

            # Clean up
            if postgres is not None:
                postgres.stop()
            if server_process is not None:
                server_process.terminate()
                server_process.wait()
            if stdout_thread is not None:
                stdout_thread.join()
            if stderr_thread is not None:
                stderr_thread.join()
            if synapse_dir is not None:
                shutil.rmtree(synapse_dir)
        except Exception as e:
            if postgres is not None:
                postgres.stop()
            if server_process is not None:
                server_process.terminate()
                server_process.wait()
            if stdout_thread is not None:
                stdout_thread.join()
            if stderr_thread is not None:
                stderr_thread.join()
            if synapse_dir is not None:
                shutil.rmtree(synapse_dir)
            raise e
