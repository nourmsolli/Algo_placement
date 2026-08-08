"""
vsphere_client.py
==================
Se connecte à vSphere (pyVmomi) et transforme les objets vSphere bruts en
objets shared.models propres.

DEUX méthodes de scan :
  - scan()           : méthode d'origine, un appel réseau par host/VM.
  - scan_optimized() : PropertyCollector, une poignée de requêtes groupées
                        au lieu de centaines/milliers d'appels individuels.
                        C'est la méthode utilisée PAR DÉFAUT (voir main.py).

Les deux renvoient exactement le même type d'objet (VsphereInventory).
"""

import ssl
import time
import logging
from pyVim import connect
from pyVmomi import vim, vmodl

from shared.models import DataStore, ESX, Cluster, VsphereInventory, AffinityRule, ResourcePool

logger = logging.getLogger(__name__)


def bytes_to_gb(value: float) -> float:
    return value / (1024 ** 3)


def mb_to_gb(value: float) -> float:
    return value / 1024


class VSphereClient:
    """Wrapper autour de pyVmomi. Une instance = une connexion à un vCenter donné."""

    def __init__(self, host: str, user: str, password: str, port: int = 443):
        self.host = host
        self.user = user
        self.password = password
        self.port = port
        self.si = None
        self.custom_attr = {}   # id -> nom des attributs personnalisés vSphere

    def connect(self) -> None:
        context = ssl._create_unverified_context()
        self.si = connect.SmartConnect(
            host=self.host, user=self.user, pwd=self.password, port=self.port, sslContext=context,
        )
        content = self.si.RetrieveContent()
        self.custom_attr = {f.key: f.name for f in content.customFieldsManager.field}

    def disconnect(self) -> None:
        if self.si:
            connect.Disconnect(self.si)
            self.si = None

    # =====================================================================
    # Helpers communs
    # =====================================================================

    def _is_host_healthy_bulk(self, overall_status, in_maintenance, custom_values) -> bool:
        try:
            activated = next(
                (c.value for c in custom_values if self.custom_attr.get(c.key) == "host_activated"), "KO"
            )
            return overall_status == "green" and not in_maintenance and activated.upper() == "OK"
        except Exception:
            return False

    def _is_cluster_activated_bulk(self, custom_values) -> bool:
        try:
            activated = next(
                (c.value for c in custom_values if self.custom_attr.get(c.key) == "cluster_activated"), "KO"
            )
            return activated.upper() == "OK"
        except Exception:
            return False

    def _read_custom_attributes_bulk(self, custom_values) -> dict[str, str]:
        attrs: dict[str, str] = {}
        try:
            for c in custom_values:
                name = self.custom_attr.get(c.key)
                if name:
                    attrs[name] = c.value
        except Exception:
            pass
        return attrs

    def _read_vm_group_names_bulk(self, rules) -> list[str]:
        names: list[str] = []
        try:
            for rule in rules or []:
                if hasattr(rule, "vmGroupName") and rule.vmGroupName:
                    names.append(rule.vmGroupName)
        except Exception:
            pass
        return names

    # =====================================================================
    # Méthode 1 (originale) : scan() — un appel réseau par objet
    # =====================================================================

    def _is_host_healthy(self, host) -> bool:
        try:
            activated = next(
                (c.value for c in host.customValue if self.custom_attr.get(c.key) == "host_activated"), "KO"
            )
            return (
                host.overallStatus == "green"
                and not host.summary.runtime.inMaintenanceMode
                and activated.upper() == "OK"
            )
        except Exception:
            return False

    def _is_cluster_activated(self, cluster) -> bool:
        try:
            activated = next(
                (c.value for c in cluster.customValue if self.custom_attr.get(c.key) == "cluster_activated"), "KO"
            )
            return activated.upper() == "OK"
        except Exception:
            return False

    def _sum_vms_resources(self, vms: list) -> tuple[int, int]:
        total_cpu, total_ram = 0, 0
        for vm in vms:
            try:
                total_cpu += vm.summary.config.numCpu
                total_ram += vm.summary.config.memorySizeMB
            except Exception:
                continue
        return total_cpu, total_ram

    def _retrieve_datastore(self, datastores: list, storage_type: str | None = None) -> DataStore | None:
        for ds in datastores:
            if "TEMPLATE" in ds.name:
                continue
            if storage_type and storage_type.upper() not in ds.name.upper():
                continue
            return DataStore(
                name=ds.name,
                capacity=bytes_to_gb(ds.summary.capacity),
                freespace=bytes_to_gb(ds.summary.freeSpace),
                accessible=ds.summary.accessible,
            )
        return None

    def _build_esx(self, host, cpu_to_vcpu_ratio: float) -> ESX | None:
        if not self._is_host_healthy(host):
            return None
        vm_cpu, vm_ram = self._sum_vms_resources(host.vm)
        esx_version = ""
        try:
            esx_version = host.config.product.version
        except Exception:
            pass
        esx = ESX(
            name=host.name,
            max_allocable_cpu=host.hardware.cpuInfo.numCpuCores * cpu_to_vcpu_ratio,
            cpu_allocated=vm_cpu,
            max_allocable_memory=bytes_to_gb(host.hardware.memorySize),
            memory_allocated=mb_to_gb(vm_ram),
            esx_version=esx_version,
        )
        esx.cpu_usage = (esx.cpu_allocated * 100 / esx.max_allocable_cpu) if esx.max_allocable_cpu else 0
        esx.memory_usage = (esx.memory_allocated * 100 / esx.max_allocable_memory) if esx.max_allocable_memory else 0
        esx.best_availability_score = esx.cpu_usage + esx.memory_usage
        esx.datastore = self._retrieve_datastore(host.datastore, "SSD")
        return esx

    def _build_affinity_rules(self, vsphere_cluster) -> list[AffinityRule]:
        rules: list[AffinityRule] = []
        try:
            if not hasattr(vsphere_cluster.configuration, "rule"):
                return rules
            for rule in vsphere_cluster.configuration.rule:
                if not hasattr(rule, "vm"):
                    continue
                member_hosts = []
                for vm in rule.vm:
                    try:
                        if vm.runtime.host:
                            member_hosts.append(vm.runtime.host.name)
                    except Exception:
                        continue
                rules.append(AffinityRule(name=rule.name, member_hosts=member_hosts))
        except Exception as e:
            logger.error(f"Erreur lecture règles d'affinité sur {vsphere_cluster.name}: {e}")
        return rules

    def _read_vm_group_names(self, vsphere_cluster) -> list[str]:
        names: list[str] = []
        try:
            if hasattr(vsphere_cluster.configuration, "rule") and isinstance(vsphere_cluster.configuration.rule, list):
                for rule in vsphere_cluster.configuration.rule:
                    if hasattr(rule, "vmGroupName") and rule.vmGroupName:
                        names.append(rule.vmGroupName)
        except Exception as e:
            logger.error(f"Erreur lecture VM groups sur {vsphere_cluster.name}: {e}")
        return names

    def _build_resource_pool(self, vsphere_cluster, cpu_to_vcpu_ratio: float) -> ResourcePool | None:
        """Reprend build_resourcepool_from_vsphere_obj : on retire le plus
        gros ESX du pool (marge de sécurité N-1 en cas de panne)."""
        try:
            hosts = list(sorted(vsphere_cluster.host, key=lambda h: h.hardware.memorySize))[:-1]
            if not hosts:
                return None
            total_memory = sum(h.hardware.memorySize for h in hosts)
            total_cores = sum(h.hardware.cpuInfo.numCpuCores for h in hosts)

            total_vm_cpu, total_vm_ram = 0, 0
            for host in vsphere_cluster.host:
                vm_cpu, vm_ram = self._sum_vms_resources(host.vm)
                total_vm_cpu += vm_cpu
                total_vm_ram += vm_ram

            rp = ResourcePool(
                name=vsphere_cluster.resourcePool.name,
                max_allocable_memory=bytes_to_gb(total_memory),
                memory_entitled=mb_to_gb(total_vm_ram),
                max_allocable_cpu=total_cores * cpu_to_vcpu_ratio,
                cpu_entitled=total_vm_cpu,
            )
            rp.memory_usage = (rp.memory_entitled * 100 / rp.max_allocable_memory) if rp.max_allocable_memory else 0
            rp.cpu_usage = (rp.cpu_entitled * 100 / rp.max_allocable_cpu) if rp.max_allocable_cpu else 0
            rp.best_availability_score = rp.memory_usage + rp.cpu_usage
            return rp
        except Exception as e:
            logger.error(f"Erreur construction resource pool sur {vsphere_cluster.name}: {e}")
            return None

    def scan(self, cpu_to_vcpu_ratio: float = 8.0) -> VsphereInventory:
        """Méthode originale : un appel réseau par host/VM. Gardée pour comparaison."""
        if not self.si:
            self.connect()

        container = self.si.content.rootFolder
        view = self.si.content.viewManager.CreateContainerView(container, [vim.ClusterComputeResource], True)
        vsphere_clusters = view.view

        clusters: list[Cluster] = []
        for vsphere_cluster in vsphere_clusters:
            try:
                if not self._is_cluster_activated(vsphere_cluster):
                    continue
                if not vsphere_cluster.host:
                    continue

                cluster = Cluster(name=vsphere_cluster.name, cpu_hz=vsphere_cluster.host[0].hardware.cpuInfo.hz)
                for host in vsphere_cluster.host:
                    if esx := self._build_esx(host, cpu_to_vcpu_ratio):
                        cluster.hosts.append(esx)

                if not cluster.hosts:
                    continue

                cluster.hosts = sorted(cluster.hosts, key=lambda h: h.best_availability_score)
                cluster.best_availability_score = cluster.hosts[0].best_availability_score
                cluster.datastore = self._retrieve_datastore(vsphere_cluster.datastore)
                cluster.affinity_rules = self._build_affinity_rules(vsphere_cluster)
                cluster.custom_attributes = self._read_custom_attributes_bulk(vsphere_cluster.customValue)
                cluster.vm_group_names = self._read_vm_group_names(vsphere_cluster)
                if rp := self._build_resource_pool(vsphere_cluster, cpu_to_vcpu_ratio):
                    cluster.resource_pool.append(rp)
                    cluster.best_availability_score = rp.best_availability_score
                clusters.append(cluster)
            except Exception as e:
                logger.error(f"Erreur pendant le scan du cluster {vsphere_cluster.name} : {e}")

        view.Destroy()
        return VsphereInventory(name=self.host, clusters=clusters)

    # =====================================================================
    # Méthode 2 (optimisée) : scan_optimized() — PropertyCollector
    # =====================================================================

    def _bulk_get_property_dict(self, prop_collector, object_specs, prop_specs) -> dict:
        """Exécute UNE requête groupée et renvoie {moref: {propriete: valeur}}."""
        filter_spec = vmodl.query.PropertyCollector.FilterSpec(objectSet=object_specs, propSet=prop_specs)
        result = prop_collector.RetrieveContents([filter_spec])
        data = {}
        for obj_content in result:
            props = {prop.name: prop.val for prop in obj_content.propSet}
            data[obj_content.obj] = props
        return data

    def scan_optimized(self, cpu_to_vcpu_ratio: float = 8.0) -> VsphereInventory:
        """
        Même résultat que scan(), mais en 4 requêtes groupées maximum
        (clusters, hosts, VMs, datastores) au lieu d'une requête par objet.

        Limite connue : les règles d'anti-affinité détaillées (affinity_rules)
        ne sont pas encore portées ici (restent vides). Utilise scan() si tu
        as besoin de l'anti-affinité en attendant.
        """
        t0 = time.time()
        if not self.si:
            self.connect()

        content = self.si.content
        prop_collector = content.propertyCollector
        root = content.rootFolder

        cluster_view = content.viewManager.CreateContainerView(root, [vim.ClusterComputeResource], True)
        host_view = content.viewManager.CreateContainerView(root, [vim.HostSystem], True)
        vm_view = content.viewManager.CreateContainerView(root, [vim.VirtualMachine], True)
        ds_view = content.viewManager.CreateContainerView(root, [vim.Datastore], True)

        clusters_mo = cluster_view.view
        hosts_mo = host_view.view
        vms_mo = vm_view.view
        datastores_mo = ds_view.view

        for v in (cluster_view, host_view, vm_view, ds_view):
            v.Destroy()

        if not clusters_mo:
            return VsphereInventory(name=self.host, clusters=[])

        cluster_props = self._bulk_get_property_dict(
            prop_collector,
            [vmodl.query.PropertyCollector.ObjectSpec(obj=o, skip=False) for o in clusters_mo],
            [vmodl.query.PropertyCollector.PropertySpec(
                type=vim.ClusterComputeResource,
                pathSet=["name", "host", "datastore", "resourcePool", "configuration.rule", "customValue"],
            )],
        )
        host_props = self._bulk_get_property_dict(
            prop_collector,
            [vmodl.query.PropertyCollector.ObjectSpec(obj=o, skip=False) for o in hosts_mo],
            [vmodl.query.PropertyCollector.PropertySpec(
                type=vim.HostSystem,
                pathSet=[
                    "name", "overallStatus", "runtime.inMaintenanceMode", "customValue",
                    "hardware.cpuInfo.numCpuCores", "hardware.cpuInfo.hz", "hardware.memorySize",
                    "datastore", "config.product.version",
                ],
            )],
        )
        vm_props = self._bulk_get_property_dict(
            prop_collector,
            [vmodl.query.PropertyCollector.ObjectSpec(obj=o, skip=False) for o in vms_mo],
            [vmodl.query.PropertyCollector.PropertySpec(
                type=vim.VirtualMachine,
                pathSet=["summary.config.numCpu", "summary.config.memorySizeMB", "runtime.host"],
            )],
        )
        ds_props = self._bulk_get_property_dict(
            prop_collector,
            [vmodl.query.PropertyCollector.ObjectSpec(obj=o, skip=False) for o in datastores_mo],
            [vmodl.query.PropertyCollector.PropertySpec(
                type=vim.Datastore,
                pathSet=["name", "summary.capacity", "summary.freeSpace", "summary.accessible"],
            )],
        )

        vm_resources_by_host: dict = {}
        for vm_mo, props in vm_props.items():
            host_mo = props.get("runtime.host")
            if not host_mo:
                continue
            cpu = props.get("summary.config.numCpu", 0) or 0
            ram = props.get("summary.config.memorySizeMB", 0) or 0
            agg = vm_resources_by_host.setdefault(host_mo, [0, 0])
            agg[0] += cpu
            agg[1] += ram

        def make_datastore(ds_mo, storage_type: str | None = None) -> DataStore | None:
            props = ds_props.get(ds_mo)
            if not props:
                return None
            name = props.get("name", "")
            if "TEMPLATE" in name:
                return None
            if storage_type and storage_type.upper() not in name.upper():
                return None
            return DataStore(
                name=name,
                capacity=bytes_to_gb(props.get("summary.capacity", 0) or 0),
                freespace=bytes_to_gb(props.get("summary.freeSpace", 0) or 0),
                accessible=bool(props.get("summary.accessible", False)),
            )

        def pick_datastore(ds_morefs, storage_type: str | None = None) -> DataStore | None:
            for ds_mo in ds_morefs or []:
                if ds := make_datastore(ds_mo, storage_type):
                    return ds
            return None

        def build_esx(host_mo, ratio: float) -> ESX | None:
            props = host_props.get(host_mo)
            if not props:
                return None
            if not self._is_host_healthy_bulk(
                props.get("overallStatus"), props.get("runtime.inMaintenanceMode", False), props.get("customValue", [])
            ):
                return None
            vm_cpu, vm_ram = vm_resources_by_host.get(host_mo, [0, 0])
            esx = ESX(
                name=props.get("name", ""),
                max_allocable_cpu=(props.get("hardware.cpuInfo.numCpuCores", 0) or 0) * ratio,
                cpu_allocated=vm_cpu,
                max_allocable_memory=bytes_to_gb(props.get("hardware.memorySize", 0) or 0),
                memory_allocated=mb_to_gb(vm_ram),
                esx_version=(props.get("config.product.version", "") or ""),
            )
            esx.cpu_usage = (esx.cpu_allocated * 100 / esx.max_allocable_cpu) if esx.max_allocable_cpu else 0
            esx.memory_usage = (esx.memory_allocated * 100 / esx.max_allocable_memory) if esx.max_allocable_memory else 0
            esx.best_availability_score = esx.cpu_usage + esx.memory_usage
            esx.datastore = pick_datastore(props.get("datastore"), "SSD")
            return esx

        clusters: list[Cluster] = []
        for cluster_mo in clusters_mo:
            cprops = cluster_props.get(cluster_mo)
            if not cprops:
                continue
            if not self._is_cluster_activated_bulk(cprops.get("customValue", [])):
                continue

            host_morefs = cprops.get("host") or []
            if not host_morefs:
                continue

            first_host_props = host_props.get(host_morefs[0], {})
            cluster = Cluster(
                name=cprops.get("name", ""),
                cpu_hz=int(first_host_props.get("hardware.cpuInfo.hz", 0) or 0),
            )
            for host_mo in host_morefs:
                if esx := build_esx(host_mo, cpu_to_vcpu_ratio):
                    cluster.hosts.append(esx)

            if not cluster.hosts:
                continue

            cluster.hosts.sort(key=lambda h: h.best_availability_score)
            cluster.best_availability_score = cluster.hosts[0].best_availability_score
            cluster.datastore = pick_datastore(cprops.get("datastore"))
            cluster.custom_attributes = self._read_custom_attributes_bulk(cprops.get("customValue", []))
            cluster.vm_group_names = self._read_vm_group_names_bulk(cprops.get("configuration.rule"))

            hosts_sorted_by_mem = sorted(
                host_morefs, key=lambda h: host_props.get(h, {}).get("hardware.memorySize", 0) or 0
            )[:-1]
            if hosts_sorted_by_mem:
                total_memory = sum(host_props.get(h, {}).get("hardware.memorySize", 0) or 0 for h in hosts_sorted_by_mem)
                total_cores = sum(host_props.get(h, {}).get("hardware.cpuInfo.numCpuCores", 0) or 0 for h in hosts_sorted_by_mem)
                total_vm_cpu = sum(vm_resources_by_host.get(h, [0, 0])[0] for h in host_morefs)
                total_vm_ram = sum(vm_resources_by_host.get(h, [0, 0])[1] for h in host_morefs)

                rp = ResourcePool(
                    name=f"{cluster.name}-rp",
                    max_allocable_memory=bytes_to_gb(total_memory),
                    memory_entitled=mb_to_gb(total_vm_ram),
                    max_allocable_cpu=total_cores * cpu_to_vcpu_ratio,
                    cpu_entitled=total_vm_cpu,
                )
                rp.memory_usage = (rp.memory_entitled * 100 / rp.max_allocable_memory) if rp.max_allocable_memory else 0
                rp.cpu_usage = (rp.cpu_entitled * 100 / rp.max_allocable_cpu) if rp.max_allocable_cpu else 0
                rp.best_availability_score = rp.memory_usage + rp.cpu_usage
                cluster.resource_pool.append(rp)
                cluster.best_availability_score = rp.best_availability_score

            clusters.append(cluster)

        elapsed = time.time() - t0
        logger.info(f"scan_optimized terminé en {elapsed:.2f}s ({len(clusters)} clusters, {len(hosts_mo)} hosts, {len(vms_mo)} VMs)")
        return VsphereInventory(name=self.host, clusters=clusters)
