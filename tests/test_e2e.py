import asyncio
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from time import perf_counter
from typing import IO, Literal, Tuple, Union
from uuid import uuid4

import aiounittest
import psycopg2
import requests
import testing.postgresql
import yaml
from psycopg2.extensions import parse_dsn

from synapse_room_code import SynapseRoomCodeConfig
from synapse_room_code.constants import (
    ACCESS_CODE_JOIN_RULE_CONTENT_KEY,
    JOIN_RULE_CONTENT_KEY,
    KNOCK_JOIN_RULE_VALUE,
    MEMBERSHIP_CONTENT_KEY,
    MEMBERSHIP_INVITE,
)
from synapse_room_code.is_rate_limited import is_rate_limited

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
        postgresql_url: Union[str, None] = None,
    ) -> Tuple[str, str, subprocess.Popen, threading.Thread, threading.Thread]:
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
            def read_output(pipe: Union[IO[str], None]):
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
        access_code: Union[str, None] = None,
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
        else:
            register_user_cmd.append("--no-admin")
        subprocess.check_call(register_user_cmd, cwd=dir)

    async def login_user(self, user: str, password: str) -> Tuple[str, str]:
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
        knock_with_code_url = (
            "http://localhost:8008/_synapse/client/pangea/v1/knock_with_code"
        )
        response = requests.post(
            knock_with_code_url,
            json={"access_code": access_code},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(response.status_code, 200)

    async def knock_without_access_token(self):
        knock_with_code_url = (
            "http://localhost:8008/_synapse/client/pangea/v1/knock_with_code"
        )
        response = requests.post(
            knock_with_code_url,
            json={"access_code": "invalid"},
        )
        self.assertEqual(response.status_code, 403)

    async def knock_with_invalid_code(self, access_token: str):
        knock_with_code_url = (
            "http://localhost:8008/_synapse/client/pangea/v1/knock_with_code"
        )
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

    async def set_room_power_levels(
        self, room_id: str, access_token: str, user_power_levels: dict
    ):
        headers = {"Authorization": f"Bearer {access_token}"}
        set_power_levels_url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/state/m.room.power_levels"
        power_levels_content = {
            "users": user_power_levels,
            "users_default": 0,
            "events": {},
            "events_default": 0,
            "state_default": 50,
            "ban": 50,
            "kick": 50,
            "redact": 50,
            "invite": 50,
        }
        response = requests.put(
            set_power_levels_url,
            json=power_levels_content,
            headers=headers,
        )
        self.assertEqual(response.status_code, 200)
        event_id = response.json()["event_id"]
        self.assertIsInstance(event_id, str)
        return event_id

    async def join_room(self, room_id: str, access_token: str):
        headers = {"Authorization": f"Bearer {access_token}"}
        join_room_url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/join"
        response = requests.post(join_room_url, json={}, headers=headers)
        self.assertEqual(response.status_code, 200)
        room_id_response = response.json()["room_id"]
        self.assertIsInstance(room_id_response, str)
        return room_id_response

    async def invite_user_to_room(self, room_id: str, user_id: str, access_token: str):
        headers = {"Authorization": f"Bearer {access_token}"}
        invite_user_url = (
            f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/invite"
        )
        response = requests.post(
            invite_user_url, json={"user_id": user_id}, headers=headers
        )
        self.assertEqual(response.status_code, 200)

    async def leave_room(self, room_id: str, access_token: str):
        headers = {"Authorization": f"Bearer {access_token}"}
        leave_room_url = (
            f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/leave"
        )
        response = requests.post(leave_room_url, json={}, headers=headers)
        self.assertEqual(response.status_code, 200)

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
            dbname = f"testdb_{uuid4().hex}"
            conn = psycopg2.connect(postgres_url)
            conn.autocommit = True
            cursor = conn.cursor()
            cursor.execute(
                f"""
                CREATE DATABASE {dbname}
                WITH TEMPLATE template0
                LC_COLLATE 'C'
                LC_CTYPE 'C';
            """
            )
            cursor.close()
            conn.close()

            # Update the connection parameters to connect to 'test_[dbname]'
            dsn_params = parse_dsn(postgres_url)
            dsn_params["dbname"] = dbname
            postgres_url_testdb = psycopg2.extensions.make_dsn(**dsn_params)

            # Confirm the collation
            conn = psycopg2.connect(postgres_url)
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT datcollate FROM pg_database WHERE datname = '{dbname}';"
            )
            collation = cursor.fetchone()[0]
            assert collation == "C", f"Expected collation 'C', got '{collation}'"
            cursor.execute(
                f"SELECT datctype FROM pg_database WHERE datname = '{dbname}';"
            )
            ctype = cursor.fetchone()[0]
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

    async def test_e2e_knock_with_code_sqlite(self) -> None:
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
            await self.knock_without_access_token()
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

    async def test_e2e_knock_with_code_admin_left_sqlite(self) -> None:
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
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="test3",
                password="123123123",
                admin=True,
            )

            # Login to obtain access token of all users
            user_1_id, user_1_access_token = await self.login_user(
                user="test1", password="123123123"
            )
            user_2_id, user_2_access_token = await self.login_user(
                user="test2", password="123123123"
            )
            user_3_id, user_3_access_token = await self.login_user(
                user="test3", password="123123123"
            )

            room_id = await self.create_private_room(user_1_access_token)

            # User 2 needs to be invited and then join the room first (required before they can leave)
            await self.invite_user_to_room(
                room_id=room_id, user_id=user_2_id, access_token=user_1_access_token
            )
            await self.join_room(room_id=room_id, access_token=user_2_access_token)

            # Set power levels: user1 = 100 (room creator), user2 = 100, user3 = 0
            await self.set_room_power_levels(
                room_id=room_id,
                access_token=user_1_access_token,
                user_power_levels={
                    user_1_id: 100,
                    user_2_id: 100,
                },
            )

            # User 2 (with highest power level besides creator) leaves the room
            await self.leave_room(room_id=room_id, access_token=user_2_access_token)

            await self.set_room_knockable_with_code(
                room_id=room_id,
                access_token=user_1_access_token,
                access_code=access_code,
            )

            # Invoke knock with code endpoint - should still work because user1 is still in the room
            await self.knock_with_code(access_code, user_3_access_token)

            # Wait for the invite - should work because user1 is still available to invite
            received_invitation = await self.wait_for_room_invitation(
                room_id=room_id,
                user_id=user_3_id,
                access_token=user_1_access_token,
            )
            if not received_invitation:
                self.fail("User 3 was not invited to the room")
            else:
                logger.info("User 3 was invited to the room successfully after admin left")

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

    async def test_e2e_knock_with_code_postgresql(self) -> None:
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

    async def test_e2e_knock_with_code_admin_left_postgresql(self) -> None:
        postgres = None
        server_process = None
        stdout_thread = None
        stderr_thread = None
        synapse_dir = None
        try:
            # Create a temporary directory for the Synapse server
            access_code = "vldcde1"
            postgres, postgres_url = await self.start_test_postgres()
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
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="test3",
                password="123123123",
                admin=True,
            )

            # Login to obtain access token of all users
            user_1_id, user_1_access_token = await self.login_user(
                user="test1", password="123123123"
            )
            user_2_id, user_2_access_token = await self.login_user(
                user="test2", password="123123123"
            )
            user_3_id, user_3_access_token = await self.login_user(
                user="test3", password="123123123"
            )

            room_id = await self.create_private_room(user_1_access_token)

            # User 2 needs to be invited and then join the room first (required before they can leave)
            await self.invite_user_to_room(
                room_id=room_id, user_id=user_2_id, access_token=user_1_access_token
            )
            await self.join_room(room_id=room_id, access_token=user_2_access_token)

            # Set power levels: user1 = 100 (room creator), user2 = 100, user3 = 0
            await self.set_room_power_levels(
                room_id=room_id,
                access_token=user_1_access_token,
                user_power_levels={
                    user_1_id: 100,
                    user_2_id: 100,
                },
            )

            # User 2 (with highest power level besides creator) leaves the room
            await self.leave_room(room_id=room_id, access_token=user_2_access_token)

            await self.set_room_knockable_with_code(
                room_id=room_id,
                access_token=user_1_access_token,
                access_code=access_code,
            )

            # Invoke knock with code endpoint - should still work because user1 is still in the room
            await self.knock_with_code(access_code, user_3_access_token)

            # Wait for the invite - should work because user1 is still available to invite
            received_invitation = await self.wait_for_room_invitation(
                room_id=room_id,
                user_id=user_3_id,
                access_token=user_1_access_token,
            )
            if not received_invitation:
                self.fail("User 3 was not invited to the room")
            else:
                print("User 3 was invited to the room successfully after admin left")

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

    async def get_access_token_without_access_code(self):
        get_access_token_url = (
            "http://localhost:8008/_synapse/client/pangea/v1/request_room_code"
        )
        response = requests.get(url=get_access_token_url)
        self.assertEqual(response.status_code, 403)

    async def get_access_token(self, access_token: str):
        t0 = perf_counter()
        get_access_token_url = (
            "http://localhost:8008/_synapse/client/pangea/v1/request_room_code"
        )
        response = requests.get(
            url=get_access_token_url,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(response.status_code, 200)
        t1 = perf_counter()
        print(f"Time taken to get access code: {t1 - t0} seconds")
        access_code = response.json()["access_code"]
        self.assertIsInstance(access_code, str)

    async def test_e2e_get_access_code_sqlite(self) -> None:
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None
        try:
            (
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse()

            # Register and login
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="test1",
                password="123123123",
                admin=True,
            )
            user_1_id, user_access_token = await self.login_user(
                user="test1", password="123123123"
            )

            # Get access code
            await self.get_access_token_without_access_code()
            await self.get_access_token(user_access_token)

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

    async def test_e2e_get_access_code_postgresql(self) -> None:
        postgres = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None
        try:
            postgres, postgres_url = await self.start_test_postgres()
            (
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse(
                db="postgresql", postgresql_url=postgres_url
            )

            # Register and login
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="test1",
                password="123123123",
                admin=True,
            )
            user_1_id, user_access_token = await self.login_user(
                user="test1", password="123123123"
            )

            # Get access code
            await self.get_access_token_without_access_code()
            await self.get_access_token(user_access_token)

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

    async def test_rate_limit(self) -> None:
        user_id = "foobar"
        config = SynapseRoomCodeConfig(
            knock_with_code_requests_per_burst=3,
            knock_with_code_burst_duration_seconds=5,
        )
        for _ in range(config.knock_with_code_requests_per_burst):
            self.assertFalse(is_rate_limited(user_id, config))
            await asyncio.sleep(1)
        self.assertTrue(is_rate_limited(user_id, config))
        await asyncio.sleep(config.knock_with_code_burst_duration_seconds + 1)
        self.assertFalse(is_rate_limited(user_id, config))
