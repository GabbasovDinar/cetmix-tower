from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from ..ssh.proxy import ProxySSHManager

MAX_SSH_PROXY_HOPS = 8


class CxTowerServer(models.Model):
    _inherit = "cx.tower.server"

    ssh_connection_route = fields.Selection(
        selection=[
            ("direct", "Direct"),
            ("proxy", "SSH Proxy"),
        ],
        string="SSH Connection Route",
        default="direct",
        required=True,
        tracking=True,
        groups="cetmix_tower_server.group_manager",
        help=(
            "Direct connects to this server from the Tower host. SSH Proxy opens "
            "the target SSH connection through another Tower server using a "
            "Paramiko direct-tcpip channel."
        ),
    )
    ssh_proxy_server_id = fields.Many2one(
        comodel_name="cx.tower.server",
        string="SSH Proxy Server",
        ondelete="restrict",
        tracking=True,
        groups="cetmix_tower_server.group_manager",
        help=(
            "Jump/bastion server used to reach this server. The proxy may itself "
            "use another proxy, allowing a multi-hop SSH chain."
        ),
    )

    @api.onchange("ssh_connection_route")
    def _onchange_ssh_connection_route(self):
        """
        When SSH Connection Route is set to Direct,
        SSH Proxy Server must be empty
        """
        for server in self:
            if server.ssh_connection_route == "direct":
                server.ssh_proxy_server_id = False

    @api.onchange("ssh_proxy_server_id")
    def _onchange_ssh_proxy_server_id(self):
        """
        When SSH Proxy Server is set,
        SSH Connection Route must be set to Proxy
        """
        for server in self:
            if server.ssh_proxy_server_id:
                server.ssh_connection_route = "proxy"

    @api.constrains("ssh_connection_route", "ssh_proxy_server_id")
    def _check_ssh_proxy_route(self):
        """
        Validate SSH proxy configuration
        """
        for server in self:
            if server.ssh_connection_route == "direct" and server.ssh_proxy_server_id:
                raise ValidationError(
                    _(
                        "SSH Proxy Server must be empty when SSH Connection Route "
                        "is Direct."
                    )
                )

            if server.ssh_connection_route == "proxy":
                # validate the entire proxy chain to catch missing proxies,
                # circular references, and excessive hop depth
                server._get_ssh_proxy_chain()

    def _get_ssh_route_servers(self):
        """
        Return the SSH route from the outermost proxy to the target server
        """
        self.ensure_one()

        target_server = self.sudo()
        proxy_servers = target_server._get_ssh_proxy_chain()

        for proxy_server in proxy_servers:
            if not proxy_server.active:
                raise ValidationError(
                    _("SSH Proxy Server %s is archived.") % proxy_server.name
                )

        return list(reversed(proxy_servers)) + [target_server]

    def _get_ssh_proxy_chain(self):
        """
        Return the SSH proxy chain from the target to the outermost proxy.

        The target server itself is not included.
        """
        self.ensure_one()

        proxies = []
        visited_server_ids = {self.id}
        current_server = self

        while current_server.ssh_connection_route == "proxy":
            proxy_server = current_server.ssh_proxy_server_id

            if not proxy_server:
                raise ValidationError(
                    _("SSH Proxy Server is not configured for server %s.")
                    % current_server.name
                )

            if proxy_server.id in visited_server_ids:
                raise ValidationError(_("Circular SSH proxy route detected."))

            visited_server_ids.add(proxy_server.id)
            proxies.append(proxy_server)

            if len(proxies) > MAX_SSH_PROXY_HOPS:
                raise ValidationError(
                    _("SSH proxy route exceeds the maximum of %s hops.")
                    % MAX_SSH_PROXY_HOPS
                )

            current_server = proxy_server

        return proxies

    def _get_ssh_client(self, raise_on_error=False, timeout=5000, skip_host_key=False):
        """
        Override to return standard Tower SSH manager or a proxy-aware
        compatible manager
        """
        self.ensure_one()

        if self.sudo().ssh_connection_route != "proxy":
            return super()._get_ssh_client(
                raise_on_error=raise_on_error,
                timeout=timeout,
                skip_host_key=skip_host_key,
            )

        server = self.sudo()
        try:
            route_servers = server._get_ssh_route_servers()
            connection_params = []

            for index, route_server in enumerate(route_servers):
                is_target = index == len(route_servers) - 1
                connection_params.append(
                    server._prepare_ssh_connection_params(
                        route_server,
                        timeout=timeout,
                        # `skip_host_key` belongs to the requested target connection
                        # proxy hops must keep using their own host key verification
                        # settings
                        skip_host_key=skip_host_key if is_target else False,
                    )
                )

            return ProxySSHManager(connection_params)

        except Exception as error:
            if raise_on_error:
                raise ValidationError(
                    _("SSH connection error %(err)s", err=error)
                ) from error
            return False, error

    def _prepare_ssh_connection_params(self, server, timeout, skip_host_key=False):
        """
        Prepare SSH connection parameters for a server
        """
        host = server.ip_v4_address or server.ip_v6_address
        if not host:
            raise ValidationError(
                _("IP address is not configured for server %s.") % server.name
            )

        host_key = server._get_secret_value("host_key")
        skip_host_key = skip_host_key or server.skip_host_key
        if not host_key and not skip_host_key:
            raise ValidationError(_("Host key not found for server %s.") % server.name)

        return {
            "host": host,
            "port": server.ssh_port,
            "username": server.ssh_username,
            "password": server._get_ssh_password(),
            "ssh_key": server._get_ssh_key(),
            "host_key": None if skip_host_key else host_key,
            "mode": server.ssh_auth_mode,
            "timeout": timeout,
        }
