from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class CxTowerServerTemplate(models.Model):
    _inherit = "cx.tower.server.template"

    ssh_connection_route = fields.Selection(
        selection=[
            ("direct", "Direct"),
            ("proxy", "SSH Proxy"),
        ],
        string="SSH Connection Route",
        default="direct",
        required=True,
        groups="cetmix_tower_server.group_manager",
        help="Default SSH route for servers created from this template.",
    )
    ssh_proxy_server_id = fields.Many2one(
        comodel_name="cx.tower.server",
        string="SSH Proxy Server",
        ondelete="restrict",
        groups="cetmix_tower_server.group_manager",
        help="Default jump/bastion server for servers created from this template.",
    )

    @api.onchange("ssh_connection_route")
    def _onchange_ssh_connection_route(self):
        """
        When SSH Connection Route is set to Direct,
        SSH Proxy Server must be empty
        """
        for template in self:
            if template.ssh_connection_route == "direct":
                template.ssh_proxy_server_id = False

    @api.onchange("ssh_proxy_server_id")
    def _onchange_ssh_proxy_server_id(self):
        """
        When SSH Proxy Server is set,
        SSH Connection Route must be set to Proxy
        """
        for template in self:
            if template.ssh_proxy_server_id:
                template.ssh_connection_route = "proxy"

    @api.constrains("ssh_connection_route", "ssh_proxy_server_id")
    def _check_ssh_proxy_configuration(self):
        """
        Check if SSH Proxy Configuration is valid
        """
        for template in self:
            if (
                template.ssh_connection_route == "proxy"
                and not template.ssh_proxy_server_id
            ):
                raise ValidationError(
                    _(
                        "SSH Proxy Server is required when SSH Connection Route "
                        "is Proxy."
                    )
                )
            if (
                template.ssh_connection_route == "direct"
                and template.ssh_proxy_server_id
            ):
                raise ValidationError(
                    _(
                        "SSH Proxy Server must be empty when SSH Connection Route "
                        "is Direct."
                    )
                )

    def action_create_server(self):
        """
        Override to set default SSH Connection Route and SSH Proxy Server
        from template
        """
        self.ensure_one()
        action = super().action_create_server()
        context = dict(action.get("context") or {})
        context.update(
            {
                "default_ssh_connection_route": self.ssh_connection_route,
                "default_ssh_proxy_server_id": self.ssh_proxy_server_id.id,
            }
        )
        action["context"] = context
        return action

    def _get_fields_tower_server(self):
        """
        Override to add SSH Connection Route and SSH Proxy Server to fields list
        """
        return super()._get_fields_tower_server() + [
            "ssh_connection_route",
            "ssh_proxy_server_id",
        ]
