import asyncio
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading

import aiounittest
import requests
import yaml

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
    async def test_e2e(self) -> None:
        # Create a temporary directory for the Synapse server
        temp_dir = tempfile.mkdtemp()
        server_process = None
        access_code = "123123a"

        try:
            # Generate Synapse config with server name 'my.domain.name'
            config_path = os.path.join(temp_dir, "homeserver.yaml")
            generate_config_cmd = [
                sys.executable,
                "-m",
                "synapse.app.homeserver",
                "--server-name",
                "my.domain.name",
                "--config-path",
                config_path,
                "--generate-config",
                "--report-stats=no",
            ]
            subprocess.check_call(generate_config_cmd)

            # Modify the config to include the module
            with open(config_path, "r") as f:
                config = yaml.safe_load(f)
            log_config_path = config.get("log_config")
            config["modules"] = [
                {"module": "synapse_room_code.SynapseRoomCode", "config": {}}
            ]
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
                cwd=temp_dir,
                text=True,
            )

            # Start threads to read stdout and stderr concurrently
            def read_output(pipe):
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
            max_wait_time = 60  # Maximum wait time in seconds
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

            # Register a 2 new user using the command-line utility
            register_user_1_cmd = [
                "register_new_matrix_user",
                "-c",
                config_path,
                "--user=test1",
                "--password=123123123",
                "--admin",
            ]
            subprocess.check_call(register_user_1_cmd, cwd=temp_dir)
            register_user_2_cmd = [
                "register_new_matrix_user",
                "-c",
                config_path,
                "--user=test2",
                "--password=123123123",
                "--admin",
            ]
            subprocess.check_call(register_user_2_cmd, cwd=temp_dir)

            # Login to obtain access token of both users
            login_url = "http://localhost:8008/_matrix/client/v3/login"
            login_data = {
                "type": "m.login.password",
                "user": "test1",
                "password": "123123123",
            }
            response = requests.post(login_url, json=login_data)
            self.assertEqual(response.status_code, 200)
            user_1_access_token = response.json()["access_token"]
            login_data["user"] = "test2"
            user_1_headers = {"Authorization": f"Bearer {user_1_access_token}"}
            response = requests.post(login_url, json=login_data)
            self.assertEqual(response.status_code, 200)
            user_2_access_token = response.json()["access_token"]
            user_2_headers = {"Authorization": f"Bearer {user_2_access_token}"}

            # Create a room with user 1
            create_room_url = "http://localhost:8008/_matrix/client/v3/createRoom"
            create_room_data = {
                "visibility": "private",
                "preset": "private_chat",
            }
            response = requests.post(
                create_room_url,
                json=create_room_data,
                headers=user_1_headers,
            )
            self.assertEqual(response.status_code, 200)
            room_id = response.json()["room_id"]

            # Set join rules to knock and without assigning an access code
            set_join_rules_url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/state/m.room.join_rules"
            # Set join rules to knock and assign an access code
            set_join_rules_data_with_access_code = {
                JOIN_RULE_CONTENT_KEY: KNOCK_JOIN_RULE_VALUE,
                ACCESS_CODE_JOIN_RULE_CONTENT_KEY: access_code,
            }
            response = requests.put(
                set_join_rules_url,
                json=set_join_rules_data_with_access_code,
                headers=user_1_headers,
            )
            self.assertEqual(response.status_code, 200)

            # Invoke knock with code endpoint
            knock_with_code_url = (
                "http://localhost:8008/_synapse/client/knock_with_code"
            )
            invalid_response = requests.post(
                knock_with_code_url,
                json={"access_code": "invalid"},
                headers=user_2_headers,
            )
            self.assertEqual(invalid_response.status_code, 400)
            valid_response = requests.post(
                knock_with_code_url,
                json={"access_code": access_code},
                headers=user_2_headers,
            )
            self.assertEqual(valid_response.status_code, 200)

            # Wait for the invite
            room_state_url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/state/m.room.member/@test2:my.domain.name"
            total_wait_time = 0
            max_wait_time = 10  # Maximum wait time in seconds
            wait_interval = 1  # Interval between checks in seconds
            received_invitation = False
            while total_wait_time < max_wait_time and not received_invitation:
                # Get the room state as user 1
                response = requests.get(room_state_url, headers=user_1_headers)
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

            if not received_invitation:
                self.fail("User 2 was not invited to the room")
            else:
                print("User 2 was invited to the room")
        finally:
            # Terminate the server process
            if server_process is not None:
                server_process.terminate()
                server_process.wait()

            # Continue with the main program without blocking
            stdout_thread.join()
            stderr_thread.join()

            # Clean up the temporary directory
            shutil.rmtree(temp_dir)
