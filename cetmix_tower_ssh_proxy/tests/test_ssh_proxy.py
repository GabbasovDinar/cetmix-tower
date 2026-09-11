from unittest.mock import MagicMock, patch

from odoo.exceptions import ValidationError

from odoo.addons.cetmix_tower_server.tests.common import TestTowerCommon

from ..ssh.proxy import ProxySSHConnection, ProxySSHManager


class TestTowerSSHProxy(TestTowerCommon):
    """Tests for SSH proxy routing and transport"""

    @classmethod
    def setUpClass(cls):
        """
        Create proxy servers and a private target server
        """
        super().setUpClass()

        # Directly reachable proxy servers used as SSH gateways.
        cls.proxy_a = cls.Server.create(
            {
                "name": "VPN Gateway A",
                "ip_v4_address": "203.0.113.10",
                "ssh_username": "tower",
                "ssh_password": "gateway-a-password",
                "ssh_auth_mode": "p",
                "host_key": "gateway-a-host-key",
            }
        )

        cls.proxy_b = cls.Server.create(
            {
                "name": "VPN Gateway B",
                "ip_v4_address": "203.0.113.11",
                "ssh_username": "tower",
                "ssh_password": "gateway-b-password",
                "ssh_auth_mode": "p",
                "host_key": "gateway-b-host-key",
            }
        )

        # Private target reachable only through Proxy A.
        cls.target = cls.Server.create(
            {
                "name": "Private Odoo",
                "ip_v4_address": "10.0.0.20",
                "ssh_username": "deploy",
                "ssh_password": "target-password",
                "ssh_auth_mode": "p",
                "skip_host_key": True,
                "ssh_connection_route": "proxy",
                "ssh_proxy_server_id": cls.proxy_a.id,
            }
        )

    def setUp(self):
        """
        Clear the SSH proxy manager cache before each test
        """
        super().setUp()

        # Each test must start with an isolated proxy connection cache.
        ProxySSHManager._connection_cache.clear()
        self.addCleanup(ProxySSHManager._connection_cache.clear)

    def _prepare_route_params(self, server, timeout=30):
        """
        Prepare connection parameters for the complete SSH route
        """
        return [
            server._prepare_ssh_connection_params(
                route_server,
                timeout=timeout,
            )
            for route_server in server._get_ssh_route_servers()
        ]

    def test_routes(self):
        """
        Test the different SSH route configurations
        """
        # Direct route contains only the requested server.
        route = self.proxy_a._get_ssh_route_servers()

        self.assertEqual(
            [server.id for server in route],
            [self.proxy_a.id],
        )

        # Single-hop route contains the proxy followed by the target.
        route = self.target._get_ssh_route_servers()

        self.assertEqual(
            [server.id for server in route],
            [self.proxy_a.id, self.target.id],
        )

        # Configure Proxy A itself to be reachable through Proxy B.
        self.proxy_a.write(
            {
                "ssh_connection_route": "proxy",
                "ssh_proxy_server_id": self.proxy_b.id,
            }
        )

        # Multi-hop route must be ordered from the outermost proxy to target.
        route = self.target._get_ssh_route_servers()

        self.assertEqual(
            [server.id for server in route],
            [
                self.proxy_b.id,
                self.proxy_a.id,
                self.target.id,
            ],
        )

        # An archived proxy must make the complete route invalid.
        self.proxy_a.active = False

        with self.assertRaises(ValidationError):
            self.target._get_ssh_route_servers()

    def test_circular_route_is_rejected(self):
        """
        Circular SSH proxy routes are rejected
        """
        # Target already uses Proxy A, so making Proxy A use Target creates:
        # Target -> Proxy A -> Target.
        with self.assertRaises(ValidationError):
            self.proxy_a.write(
                {
                    "ssh_connection_route": "proxy",
                    "ssh_proxy_server_id": self.target.id,
                }
            )

    def test_max_proxy_hops_is_enforced(self):
        """
        SSH proxy routes exceeding the hop limit are rejected
        """
        # Create enough proxy servers to reach the maximum allowed route depth.
        proxies = self.Server.create(
            [
                {
                    "name": f"Proxy {index}",
                    "ip_v4_address": f"198.51.100.{index + 10}",
                    "ssh_username": "tower",
                    "ssh_password": "password",
                    "ssh_auth_mode": "p",
                    "skip_host_key": True,
                }
                for index in range(9)
            ]
        )

        # Build an allowed chain of eight proxy hops:
        # Proxy 0 -> Proxy 1 -> ... -> Proxy 8.
        for index in reversed(range(8)):
            proxies[index].write(
                {
                    "ssh_connection_route": "proxy",
                    "ssh_proxy_server_id": proxies[index + 1].id,
                }
            )

        # Adding another target in front creates nine proxy hops and must fail.
        with self.assertRaises(ValidationError):
            self.Server.create(
                {
                    "name": "Too Deep Target",
                    "ip_v4_address": "10.0.0.40",
                    "ssh_username": "deploy",
                    "ssh_password": "password",
                    "ssh_auth_mode": "p",
                    "skip_host_key": True,
                    "ssh_connection_route": "proxy",
                    "ssh_proxy_server_id": proxies[0].id,
                }
            )

    def test_cache_key_separates_overlapping_private_addresses(self):
        """
        Identical private addresses behind different proxies use different keys
        """
        # Create another target with the same private IP but behind Proxy B.
        target_b = self.Server.create(
            {
                "name": "Another Private Odoo",
                "ip_v4_address": "10.0.0.20",
                "ssh_username": "deploy",
                "ssh_password": "target-password",
                "ssh_auth_mode": "p",
                "skip_host_key": True,
                "ssh_connection_route": "proxy",
                "ssh_proxy_server_id": self.proxy_b.id,
            }
        )

        # Build complete connection parameters for both routes.
        params_a = self._prepare_route_params(self.target)
        params_b = self._prepare_route_params(target_b)

        # The target IP is identical, but the routes are different and therefore
        # must never share the same cached SSH manager.
        self.assertNotEqual(
            ProxySSHManager._get_cache_key(params_a),
            ProxySSHManager._get_cache_key(params_b),
        )

    def test_proxy_connection_passes_channel_as_sock(self):
        """
        Proxy connection passes the Paramiko channel as its socket
        """
        channel = MagicMock()
        client = MagicMock()

        # Replace Paramiko SSHClient while keeping the real ProxySSHConnection.
        with patch(
            "odoo.addons.cetmix_tower_ssh_proxy.ssh.proxy.SSHClient",
            return_value=client,
        ):
            connection = ProxySSHConnection(
                host="10.0.0.20",
                port=22,
                username="deploy",
                password="password",
                host_key=None,
                mode="p",
                sock=channel,
                timeout=30,
            )

            connection_result = connection.connect()

        # ProxySSHConnection must return the created Paramiko client.
        self.assertIs(
            connection_result,
            client,
        )

        connect_params = client.connect.call_args.kwargs

        # The existing direct-tcpip channel must be passed as Paramiko's socket.
        self.assertIs(
            connect_params["sock"],
            channel,
        )
        self.assertEqual(
            connect_params["hostname"],
            "10.0.0.20",
        )
        self.assertEqual(
            connect_params["port"],
            22,
        )

    def test_proxy_connection_closes_channel(self):
        """
        Disconnect closes the Paramiko proxy channel
        """
        channel = MagicMock()

        # Create a proxy connection with an already opened direct-tcpip channel.
        connection = ProxySSHConnection(
            host="10.0.0.20",
            port=22,
            username="deploy",
            password="password",
            mode="p",
            sock=channel,
            timeout=30,
        )

        # Disconnect must release the proxy channel even if SSH was not connected.
        connection.disconnect()

        channel.close.assert_called_once_with()
        self.assertIsNone(connection.sock)

    def test_failed_proxy_connection_closes_ssh_client(self):
        """
        Failed target connection closes its temporary SSH client
        """
        channel = MagicMock()
        client = MagicMock()

        # Simulate a failure while Paramiko establishes the proxied SSH session.
        client.connect.side_effect = RuntimeError("Connection failed")

        with patch(
            "odoo.addons.cetmix_tower_ssh_proxy.ssh.proxy.SSHClient",
            return_value=client,
        ):
            connection = ProxySSHConnection(
                host="10.0.0.20",
                port=22,
                username="deploy",
                password="password",
                mode="p",
                sock=channel,
                timeout=30,
            )

            with self.assertRaises(RuntimeError):
                connection.connect()

        # Failed connections must not leave a reusable broken SSH client.
        client.close.assert_called_once_with()
        self.assertIsNone(connection._ssh_client)

    def test_proxy_manager_opens_direct_tcpip_channel(self):
        """
        Proxy manager opens direct-tcpip channel to the target
        """
        connection_params = self._prepare_route_params(self.target)

        # Mock the directly reachable proxy connection.
        proxy_connection = MagicMock()
        transport = MagicMock()
        channel = MagicMock()

        proxy_connection.get_transport.return_value = transport
        transport.open_channel.return_value = channel

        # Mock the target connection created on top of the proxy channel.
        target_connection = MagicMock()

        with (
            patch(
                "odoo.addons.cetmix_tower_ssh_proxy.ssh.proxy.SSHConnection",
                return_value=proxy_connection,
            ),
            patch(
                "odoo.addons.cetmix_tower_ssh_proxy.ssh.proxy.ProxySSHConnection",
                return_value=target_connection,
            ) as proxy_connection_class,
        ):
            manager = ProxySSHManager(connection_params)

        # Proxy transport must open a TCP channel to the actual target address.
        transport.open_channel.assert_called_once_with(
            "direct-tcpip",
            ("10.0.0.20", 22),
            ("127.0.0.1", 0),
        )

        # The target connection must be created using the opened proxy channel.
        proxy_connection_class.assert_called_once()

        target_connect_params = proxy_connection_class.call_args.kwargs

        self.assertIs(
            target_connect_params["sock"],
            channel,
        )

        # The manager exposes the target as its main SSH connection while retaining
        # the proxy connection for the lifetime of the route.
        self.assertIs(
            manager.connection,
            target_connection,
        )
        self.assertEqual(
            manager.proxy_connections,
            [proxy_connection],
        )

    def test_proxy_manager_builds_multi_hop_connection(self):
        """
        Proxy manager opens one direct-tcpip channel for each next hop
        """
        # Build route:
        # Tower -> Proxy B -> Proxy A -> Target.
        self.proxy_a.write(
            {
                "ssh_connection_route": "proxy",
                "ssh_proxy_server_id": self.proxy_b.id,
            }
        )

        connection_params = self._prepare_route_params(self.target)

        # Outermost connection: Tower -> Proxy B.
        outer_connection = MagicMock()
        outer_transport = MagicMock()
        outer_channel = MagicMock()

        outer_connection.get_transport.return_value = outer_transport
        outer_transport.open_channel.return_value = outer_channel

        # Inner connection: Proxy B -> Proxy A.
        inner_connection = MagicMock()
        inner_transport = MagicMock()
        target_channel = MagicMock()

        inner_connection.get_transport.return_value = inner_transport
        inner_transport.open_channel.return_value = target_channel

        # Final connection: Proxy A -> Target.
        target_connection = MagicMock()

        with (
            patch(
                "odoo.addons.cetmix_tower_ssh_proxy.ssh.proxy.SSHConnection",
                return_value=outer_connection,
            ),
            patch(
                "odoo.addons.cetmix_tower_ssh_proxy.ssh.proxy.ProxySSHConnection",
                side_effect=[
                    inner_connection,
                    target_connection,
                ],
            ),
        ):
            manager = ProxySSHManager(connection_params)

        # Proxy B opens the first channel to Proxy A.
        outer_transport.open_channel.assert_called_once_with(
            "direct-tcpip",
            ("203.0.113.10", 22),
            ("127.0.0.1", 0),
        )

        # Proxy A opens the second channel to the target.
        inner_transport.open_channel.assert_called_once_with(
            "direct-tcpip",
            ("10.0.0.20", 22),
            ("127.0.0.1", 0),
        )

        # Both proxy connections must remain owned by the manager.
        self.assertEqual(
            manager.proxy_connections,
            [
                outer_connection,
                inner_connection,
            ],
        )

        # Commands and SFTP must use the final target connection.
        self.assertIs(
            manager.connection,
            target_connection,
        )

    def test_proxy_manager_reuses_cached_instance(self):
        """
        Identical SSH routes reuse the cached manager instance
        """
        connection_params = self._prepare_route_params(self.target)

        # Mock a complete single-hop proxy route.
        proxy_connection = MagicMock()
        target_connection = MagicMock()
        transport = MagicMock()

        proxy_connection.get_transport.return_value = transport
        transport.open_channel.return_value = MagicMock()

        with (
            patch(
                "odoo.addons.cetmix_tower_ssh_proxy.ssh.proxy.SSHConnection",
                return_value=proxy_connection,
            ) as ssh_connection_class,
            patch(
                "odoo.addons.cetmix_tower_ssh_proxy.ssh.proxy.ProxySSHConnection",
                return_value=target_connection,
            ) as proxy_connection_class,
        ):
            # Request the same SSH route twice.
            manager_1 = ProxySSHManager(connection_params)
            manager_2 = ProxySSHManager(connection_params)

        # Both calls must return exactly the same cached manager.
        self.assertIs(
            manager_1,
            manager_2,
        )

        # Connections must only be built once.
        ssh_connection_class.assert_called_once()
        proxy_connection_class.assert_called_once()

    def test_proxy_manager_failure_is_not_cached(self):
        """
        Failed SSH route initialization does not leave a cached manager
        """
        connection_params = self._prepare_route_params(self.target)
        cache_key = ProxySSHManager._get_cache_key(connection_params)

        proxy_connection = MagicMock()

        # Simulate failure while establishing the directly reachable proxy.
        proxy_connection.get_transport.side_effect = RuntimeError(
            "Proxy connection failed"
        )

        with patch(
            "odoo.addons.cetmix_tower_ssh_proxy.ssh.proxy.SSHConnection",
            return_value=proxy_connection,
        ):
            with self.assertRaises(RuntimeError):
                ProxySSHManager(connection_params)

        # A partially initialized manager must never remain in the cache.
        self.assertNotIn(
            cache_key,
            ProxySSHManager._connection_cache,
        )

        # Already created route resources must be cleaned up.
        proxy_connection.disconnect.assert_called_once_with()

    def test_proxy_manager_disconnect_clears_cache(self):
        """
        Disconnect closes route connections and removes cached manager
        """
        connection_params = self._prepare_route_params(self.target)
        cache_key = ProxySSHManager._get_cache_key(connection_params)

        # Mock a complete proxy -> target route.
        proxy_connection = MagicMock()
        target_connection = MagicMock()
        transport = MagicMock()

        proxy_connection.get_transport.return_value = transport
        transport.open_channel.return_value = MagicMock()

        with (
            patch(
                "odoo.addons.cetmix_tower_ssh_proxy.ssh.proxy.SSHConnection",
                return_value=proxy_connection,
            ),
            patch(
                "odoo.addons.cetmix_tower_ssh_proxy.ssh.proxy.ProxySSHConnection",
                return_value=target_connection,
            ),
        ):
            manager = ProxySSHManager(connection_params)

        # Successfully initialized routes must be cached.
        self.assertIn(
            cache_key,
            ProxySSHManager._connection_cache,
        )

        manager.disconnect()

        # Disconnect must invalidate the cache entry.
        self.assertNotIn(
            cache_key,
            ProxySSHManager._connection_cache,
        )

        # Both target and proxy connections must be released.
        target_connection.disconnect.assert_called_once_with()
        proxy_connection.disconnect.assert_called_once_with()

    def test_command_runs_on_target_through_multi_hop_proxy(self):
        """
        Command is executed on the target through the complete SSH proxy route
        """
        # Build route:
        # Tower -> Proxy B -> Proxy A -> Target.
        self.proxy_a.write(
            {
                "ssh_connection_route": "proxy",
                "ssh_proxy_server_id": self.proxy_b.id,
            }
        )

        command = self.Command.create(
            {
                "name": "Proxy Target Command",
                "code": "echo proxy-target",
                "action": "ssh_command",
            }
        )

        # Outermost proxy connection: Tower -> Proxy B.
        outer_connection = MagicMock()
        outer_transport = MagicMock()
        outer_channel = MagicMock()

        outer_connection.get_transport.return_value = outer_transport
        outer_transport.open_channel.return_value = outer_channel

        # Inner proxy connection: Proxy B -> Proxy A.
        inner_client = MagicMock()
        inner_transport = MagicMock()
        target_channel = MagicMock()

        inner_client.get_transport.return_value = inner_transport
        inner_transport.open_channel.return_value = target_channel

        # Target connection: Proxy A -> Target.
        target_client = MagicMock()

        stdin = MagicMock()
        stdout = MagicMock()
        stderr = MagicMock()

        stdout.channel.recv_exit_status.return_value = 0
        stdout.readlines.return_value = ["proxy-target\n"]
        stderr.readlines.return_value = []

        target_client.exec_command.return_value = (
            stdin,
            stdout,
            stderr,
        )

        with (
            patch(
                "odoo.addons.cetmix_tower_ssh_proxy.ssh.proxy.SSHConnection",
                return_value=outer_connection,
            ),
            patch(
                "odoo.addons.cetmix_tower_ssh_proxy.ssh.proxy.SSHClient",
                side_effect=[
                    inner_client,
                    target_client,
                ],
            ),
        ):
            result = self.target.with_context(no_command_log=True).run_command(command)

            self.assertEqual(result["status"], 0)

        # Tower connects to the outermost proxy and opens a channel to Proxy A.
        outer_transport.open_channel.assert_called_once_with(
            "direct-tcpip",
            ("203.0.113.10", 22),
            ("127.0.0.1", 0),
        )

        # Proxy A SSH connection is established through Proxy B.
        inner_connect_params = inner_client.connect.call_args.kwargs

        self.assertIs(
            inner_connect_params["sock"],
            outer_channel,
        )
        self.assertEqual(
            inner_connect_params["hostname"],
            "203.0.113.10",
        )
        self.assertEqual(
            inner_connect_params["port"],
            22,
        )

        # Proxy A opens the final channel to the target.
        inner_transport.open_channel.assert_called_once_with(
            "direct-tcpip",
            ("10.0.0.20", 22),
            ("127.0.0.1", 0),
        )

        # Target SSH connection is established through the final proxy channel.
        target_connect_params = target_client.connect.call_args.kwargs

        self.assertIs(
            target_connect_params["sock"],
            target_channel,
        )
        self.assertEqual(
            target_connect_params["hostname"],
            "10.0.0.20",
        )
        self.assertEqual(
            target_connect_params["port"],
            22,
        )

        # The command must be executed only on the target connection.
        target_client.exec_command.assert_called_once_with("echo proxy-target")
        inner_client.exec_command.assert_not_called()
