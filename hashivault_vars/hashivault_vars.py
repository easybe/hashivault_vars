from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import urllib3
# import base64
# from pretty_json import format_json
import os
import socket
import hvac
from ansible.inventory.group import Group
from ansible.inventory.host import Host
from ansible.plugins.vars import BaseVarsPlugin
from ansible.utils.vars import combine_vars
from ansible.errors import AnsibleInternalError

DOCUMENTATION = '''
    vars: hashivault_vars
    version_added: "2.7"
    short_description: Lookup secrets/creds in Hashicorp Vault in group/domain/host precedence order
'''

urllib3.disable_warnings()  # suppress InsecureRequestWarning

# cache for vault lookups, keyed by folder
vault_cache = {}
authenticated = False
v_client = None


def debug(*args):
    if os.environ.get('HASHIVAULT_VARS_DEBUG') == "1":
        print("HASHIVAULT_VARS> " + "".join(map(str, args)))


debug("hashivault_vars loaded")


class VarsModule(BaseVarsPlugin):
    """
    Hashicorp Vault Vars Plugin.

    Root path in vault:
        /secret/ansible/

    Precendence (applied top to bottom, so last takes precendence):
        Groups:
            /secret/ansible/groups/all
            /secret/ansible/groups/ungrouped
            /secret/ansible/groups/your_inv_item_group
            ...

        Hosts/Domains:
            /secret/ansible/{connection}/domains/com
            /secret/ansible/{connection}/domains/example.com
            /secret/ansible/{connection}/hosts/hosta.example.com
        where {connection} is ansible_connection, e.g.: "ssh", "winrm", ...

    All values retrieved from these paths are mapped as ansible variables,
    e.g. ansible_user, ansible_password, etc.

    The layered lookups are merged, with the last taking precendence over
    earlier lookups.

    Lookups to the vault are cached for the run.
    """

    def __init__(self):
        debug("in __init__")
        super(BaseVarsPlugin, self).__init__()

        self.vault_addr = None
        if os.environ.get('VAULT_ADDR') is not None:
            self.vault_addr = os.environ.get('VAULT_ADDR')
        debug("vault_addr:", self.vault_addr)
        if self.vault_addr is None:
            debug("VAULT_ADDR isnt set, disabling hashivault_vars plugin")
            return

        self.vault_token = ""
        if os.environ.get('VAULT_TOKEN') is not None:
            self.vault_token = os.environ.get('VAULT_TOKEN')
        # debug("vault_token:", vault_token)

        self.vault_skip_verify = False
        if os.environ.get('VAULT_SKIP_VERIFY') is not None:
            self.vault_skip_verify = os.environ.get('VAULT_SKIP_VERIFY') == '1'

        # authenticated = False
        # v_client = None

    def _authenticate(self):
        """Authenticate with the vault and establish the client api"""
        global v_client, authenticated

        if v_client is None:
            debug("AUTHENTICATING TO VAULT +++++++++++++++++++")
            v_client = hvac.Client(
                url=self.vault_addr,
                token=self.vault_token,
                verify=self.vault_skip_verify
                )
            debug("after hvac.Client v_client=", v_client)
            try:
                authenticated = v_client.is_authenticated()
            except Exception as e:
                print("Error: Failed to authenticate with Vault:", e)
            else:
                debug("authenticated to vault ok")

    # See https://stackoverflow.com/questions/319279/how-to-validate-ip-address-in-python

    def _is_valid_ipv4_address(self, address):
        """Test if address is an ipv4 address."""
        try:
            socket.inet_pton(socket.AF_INET, address)
        except AttributeError:  # no inet_pton here, sorry
            try:
                socket.inet_aton(address)
            except socket.error:
                return False
            return address.count('.') == 3
        except socket.error:  # not a valid address
            return False
        return True

    def _is_valid_ipv6_address(self, address):
        """Test if address is an ipv6 address."""
        try:
            socket.inet_pton(socket.AF_INET6, address)
        except socket.error:  # not a valid address
            return False
        return True

    def _is_valid_ip_address(self, address):
        """Test if address is an ipv4 or ipv6 address."""
        if self._is_valid_ipv4_address(address):
            return True
        return self._is_valid_ipv6_address(address)

    def _read_vault(self, folder, entity_name):
        """Read a secret from a folder in Hashicorp Vault.

        Arguments:
            folder      -- Vault folder to read
            entity_name -- Secret name to read from folder

        Returns:
            Dictionary of result data from vault
        """
        global vault_cache
        key = "%s/%s" % (folder, entity_name)

        cached_value = vault_cache.get(key)
        if cached_value is not None:
            debug("_read_vault (cached) %s: %s" % (key, cached_value))
            return cached_value

        self._authenticate()
        if not authenticated:
            debug("get_vars not authenticated to vault, skipping vault lookups")
            return {}
        result = v_client.read(
            path="secret/ansible/%s" % (key)
        )
        debug("_read_vault result:", result)
        data = {}
        if result:
            data = result["data"]
        vault_cache[key] = data
        debug("_read_vault %s: %s" % (key, data))
        return data

    def _get_vars(self, data, entity):
        """Resolve lookup for vars from Vault.

        Arguments:
            data -- dict to accumulate vars into
            entity -- Ansible Group or Host entity to lookup for

        Returns:
            Dictionary of combined / overlayed vars values.
        """
        folder = ""
        if isinstance(entity, Group):
            folder = "groups"
        elif isinstance(entity, Host):
            # Resolve default connection details
            if entity.vars.get("ansible_port") is None:
                if entity.vars.get("ansible_connection") is None:
                    data["ansible_port"] = 22
            else:
                data["ansible_port"] = entity.vars.get("ansible_port")

            if entity.vars.get("ansible_connection") is None:
                if data["ansible_port"] == 5985 or data["ansible_port"] == 5986:
                    data["ansible_connection"] = "winrm"
                else:
                    data["ansible_connection"] = "ssh"
            else:
                data["ansible_connection"] = entity.vars.get(
                    "ansible_connection")

            folder = "%s/hosts" % (data["ansible_connection"])

            if not self._is_valid_ip_address(entity.name):
                parts = entity.name.split('.')
                if len(parts) == 1:
                    pass

                elif len(parts) > 1:
                    folder = "%s/domains" % (data["ansible_connection"])
                    # Loop lookups from domain-root to fqdn
                    parts.reverse()
                    prev_part = ""
                    for part in parts:
                        lookup_part = part + prev_part
                        if lookup_part == entity.name:
                            folder = "%s/hosts" % (data["ansible_connection"])
                        data = combine_vars(
                            data,
                            self._read_vault(folder, lookup_part)
                        )
                        prev_part = '.' + part + prev_part
                    return data
                else:
                    raise AnsibleInternalError(
                        "Failed to extract host name parts, len: %d", len(parts))

        else:
            raise AnsibleInternalError(
                "Unrecognised entity type encountered in hashivault_vars plugin: %s", type(entity))

        return combine_vars(data, self._read_vault(folder, entity.name))

    def get_vars(self, loader, path, entities):
        """Entry point called from Ansible to get vars."""

        debug("get_vars **********************************")
        if not isinstance(entities, list):
            entities = [entities]
        debug("lookup entities:", entities)

        super(VarsModule, self).get_vars(loader, path, entities)

        data = {}
        for entity in entities:
            data = self._get_vars(data, entity)

        debug("get_vars: ", data)
        return data
