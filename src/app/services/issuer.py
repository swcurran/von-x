from app.services import eventloop
from app.services.exchange import Exchange, ExchangeError, RequestProcessor, RequestExecutor
from app.services.tob import TobClient
from app.services.von import VonClient

from von_agent.schema import schema_key_for
from von_agent.util import encode
import json
import os
import logging
logger = logging.getLogger(__name__)


def claim_value_pair(plain):
    return [str(plain), encode(plain)]

def encode_claim(claim):
    encoded_claim = {}
    for key, value in claim.items():
        encoded_claim[key] = claim_value_pair(value) if value else \
            claim_value_pair("")
    return encoded_claim


class IssuerError(ExchangeError):
    def __init__(self, value, exc_info=None):
        super(IssuerError, self).__init__(value, exc_info)

class IssuerStatus:
    def __init__(self, status):
        self.value = status

class SubmitClaimRequest:
    def __init__(self, schema_name, schema_version, attributes):
        self.schema_name = schema_name
        self.schema_version = schema_version
        self.attributes = attributes

class SubmitClaimResponse:
    def __init__(self, value):
        self.value = value


def init_issuer_manager(config, env=None, exchange=None, pid='issuer-manager'):
    if not config:
        raise ValueError('Missing configuration for issuer manager')
    if not 'issuers' in config:
        raise ValueError('No issuers defined by configuration')
    if not env:
        env = os.environ
    if not exchange:
        logger.info('Starting new Exchange service for issuer manager')
        exchange = Exchange()
        exchange.start()
    issuers = []
    issuer_ids = []
    limit_issuers = env.get('ISSUERS')
    limit_issuers = limit_issuers.split() \
        if (limit_issuers and limit_issuers != 'all') \
        else None
    for issuer_key, issuer in config['issuers'].items():
        if not 'id' in issuer:
            issuer['id'] = issuer_key
        if not limit_issuers or issuer['id'] in limit_issuers:
            issuers.append(issuer)
            issuer_ids.append(issuer['id'])
    if len(issuers):
        logger.info('Initializing processor for services: {}'.format(', '.join(issuer_ids)))
        return IssuerManager(pid, exchange, env, issuers)
    else:
        raise ValueError('No defined issuers referenced by ISSUERS')


# There should only be one instance of this class in the application.
# It is responsible for starting the issuer services and directing
# schema requests to the right issuer
class IssuerManager(RequestProcessor):
    def __init__(self, pid, exchange, env, issuer_specs):
        super(IssuerManager, self).__init__(pid, exchange)
        self._env = env or {}
        self._issuers = {}
        self._issuer_specs = issuer_specs
        self._issuer_status = {}
        self._orgbook_did = None
        self._ready = False

    def ready(self):
        return self._ready

    def status(self):
        return {
            'issuers': self._issuer_status.copy(),
            'orgbook_did': self._orgbook_did,
            'ready': self._ready,
            'version': self._env.get('VERSION')
        }

    def start(self):
        ret = super(IssuerManager, self).start()
        self._start_services()
        return ret

    def stop(self):
        self._stop_services()
        super(IssuerManager, self).stop()

    def _start_services(self):
        async def resolve():
            try:
                await self.resolve_orgbook_did()
            except:
                errmsg = IssuerError('Error while resolving DID for TOB', True)
                self.send_noreply(self.get_pid(), errmsg)
                raise
            try:
                self.start_issuers()
            except:
                errmsg = IssuerError('Error while starting issuer services', True)
                self.send_noreply(self.get_pid(), errmsg)
                raise
        eventloop.run_in_thread(resolve())

    def _stop_services(self):
        self.stop_issuers()

    # Resolve DID for orgbook from given seed if necessary
    async def resolve_orgbook_did(self):
        if not self._orgbook_did:
            tob_did = self._env.get('TOB_INDY_DID')
            if not tob_did:
                tob_seed = self._env.get('TOB_INDY_SEED')
                if not tob_seed:
                    raise ValueError('Either TOB_INDY_SEED or TOB_INDY_DID must be defined')
                logger.info('Resolving TOB DID from seed {}'.format(tob_seed))
                # create 'blank' client with no issuer information
                von_client = self.init_von_client()
                tob_did = await von_client.resolve_did_from_seed(tob_seed)
                if not tob_did:
                    raise ValueError('DID for TOB could not be resolved')
                self._orgbook_did = tob_did
                logger.info('Resolved TOB DID to {}'.format(tob_did))
        return self._orgbook_did

    def extend_issuer_spec(self, spec):
        spec = spec.copy() if spec else {}
        if not 'genesis_path' in spec:
            spec['genesis_path'] = self._env.get('INDY_GENESIS_PATH')
        if not 'ledger_url' in spec:
            spec['ledger_url'] = self._env.get('INDY_LEDGER_URL')
        if not 'api_url' in spec:
            spec['api_url'] = self._env.get('TOB_API_URL')
        spec['api_did'] = self._orgbook_did
        return spec

    def init_von_client(self):
        cfg = {
            'genesis_path': self._env.get('INDY_GENESIS_PATH'),
            'ledger_url': self._env.get('INDY_LEDGER_URL')
        }
        return VonClient(cfg)

    def start_issuers(self):
        logger.info('Starting issuers')
        for spec in self._issuer_specs:
            service = IssuerService(self.get_exchange(), self.extend_issuer_spec(spec), self.get_pid())
            self._issuers[service.id] = service
        for id, service in self._issuers.items():
            service.start() # or start_process()

    def stop_issuers(self):
        for id, service in self._issuers.items():
            service.stop()

    def find_issuer_for_schema(self, schema_name, schema_version=None):
        for id, service in self._issuers.items():
            if service.find_claim_type_for_schema(schema_name, schema_version):
                return service.id

    def process(self, from_pid, ident, message, ref):
        if isinstance(message, IssuerError):
            logger.error(message.format())
        elif isinstance(message, IssuerStatus):
            self._issuer_status[from_pid] = message.value
            self.update_status()
        elif message == 'ready':
            self.send_noreply(from_pid, self.ready(), ident)
        elif message == 'status':
            self.send_noreply(from_pid, self.status(), ident)
        else:
            raise ValueError('Unexpected message from {}: {}'.format(from_pid, message))

    def update_status(self):
        ok = True
        old_ok = self._ready
        for id, handle in self._issuers.items():
            if not self._issuer_status.get(id, {}).get('ready'):
                ok = False
                break
        self._ready = ok
        if ok and not old_ok:
            logger.info('Completed issuer manager initialization')


# These instances are normally initialized by the InstanceManager.
# They listen for requests and perform each one in a thread pool.
class IssuerService(RequestExecutor):
    def __init__(self, exchange, spec=None, manager_pid=None):
        self.id = None
        self._config = {}
        self._status = {}
        self._manager_pid = manager_pid
        self._orgbook_did = None
        self._update_config(spec)
        self._von_client = None

        super(IssuerService, self).__init__(self.id, exchange)
        self._update_status({
            'id': self.id,
            'did': None,
            'ledger': False,
            'orgbook': False,
            'ready': False,
            'syncing': False
        })

    def _update_config(self, spec):
        if spec:
            self._config.update(spec)
        if 'id' in self._config:
            self.id = self._config['id']
        if 'did' in self._config:
            self._status['did'] = self._config['did']
        if 'api_did' in self._config:
            self._orgbook_did = self._config['api_did']

    def _update_status(self, update=None, silent=False):
        if update:
            self._status.update(update)
        if self._manager_pid and not silent:
            self.send_noreply(self._manager_pid, IssuerStatus(self._status))

    def ready(self):
        return self._status['ready']

    def start(self):
        try:
            ret = super(IssuerService, self).start()
            async def init():
                try:
                    await self.sync()
                except:
                    if self._manager_pid:
                        errmsg = IssuerError('Exception during issuer sync', True)
                        self.send_noreply(self._manager_pid, errmsg)
                    else:
                        logger.exception('Exception during issuer sync:')
            # Start another thread to perform initial sync
            eventloop.run_in_executor(self.get_pool(), init())
            return ret
        except:
            logger.exception('Error starting issuer service:')

    # Sync with issuer VON client, then TOB client
    async def sync(self):
        self._update_status({
            'syncing': True
        })
        try:
            von_client = self.init_von_client()
            await von_client.sync()
            self._update_status({
                'did': von_client.issuer_did,
                'ledger': von_client.synced
            })
            if von_client.synced:
                tob_client = self.init_tob_client()
                await tob_client.sync()
                self._update_status({
                    'orgbook': tob_client.synced
                })
            if self._status['ledger'] and self._status['orgbook']:
                self._update_status({'ready': True, 'syncing': False})
            return self._status['ready']
        except:
            self._update_status({'ready': False, 'syncing': False})
            raise

    def init_von_client(self):
        if not self._von_client:
            cfg = self._config.copy()
            self._von_client = VonClient(cfg)
        return self._von_client

    def init_tob_client(self, spec=None):
        cfg = self._config.copy()
        cfg['did'] = self._status['did']
        return TobClient(cfg)

    def find_claim_type_for_schema(self, schema_name, schema_version=None):
        ctypes = self._config.get('claim_types')
        if ctypes:
            for ctype in ctypes:
                if 'schema' in ctype and ctype['schema']['name'] == schema_name \
                        and (not schema_version or ctype['schema']['version'] == schema_version):
                    return ctype

    def process(self, from_pid, ident, message, ref):
        eventloop.run_in_executor(self.get_pool(), self.handle_request(from_pid, ident, message))

    async def handle_request(self, from_pid, ident, message):
        try:
            if isinstance(message, SubmitClaimRequest):
                try:
                    result = await self.submit_claim(
                            message.schema_name,
                            message.schema_version,
                            message.attributes)
                    return self.send_noreply(from_pid, SubmitClaimResponse(result), ident)
                except:
                    errmsg = IssuerError('Exception during claim submission', True)
                    self.send_noreply(from_pid, errmsg, ident)
            else:
                raise ValueError('Unrecognized request type')
        except:
            errmsg = IssuerError('Exception during issuer request handling', True)
            return self.send_noreply(from_pid, errmsg, ident)

    def load_claim_request(self, claim_type, request):
        # Build schema body skeleton
        claim = {}
        for attr in claim_type['schema']['attr_names']:
            claim[attr] = None

        mapping = claim_type.get('mapping')
        if not mapping:
            # Default to copying schema attributes by name if no mapping is provided
            for attr in claim_type['schema']['attr_names']:
                claim[attr] = request.get(attr)
        else:
            # Build claim data from schema mapping
            for attribute in mapping:
                attr_name = attribute.get('name')
                from_type = attribute.get('from', 'request')
                # Handle getting value from request data
                if from_type == 'request':
                    source = attribute.get('source', attr_name)
                    claim[attr_name] = request.get(source)
                # Handle getting value from helpers (function defined in config)
                elif from_type == 'helper':
                    #try:
                    #    helpers = import_module('von_connector.helpers')
                    #    helper = getattr(helpers, attribute['source'])
                    #    claim[attribute['name']] = helper()
                    #except AttributeError:
                    #    raise Exception(
                    #        'Cannot find helper "%s"' % attribute['source'])
                    pass
                # Handle setting value with string literal or None
                elif from_type == 'literal':
                    claim[attr_name] = attribute.get('source')
                # Handle getting value already set on schema skeleton
                elif from_type == 'previous':
                    source = attribute.get('source')
                    if source:
                        try:
                            claim[attr_name] = claim[source]
                        except KeyError:
                            raise ValueError(
                                'Cannot find previous value "%s"' % source)
                else:
                    raise ValueError('Unkown mapping type "%s"' % attribute['from'])
        return claim

    async def _create_issuer_claim_def(self, issuer, schema_def):
        self.__log_json('Schema definition:', schema_def)

        # We need schema from ledger
        schema_json = await issuer.get_schema(
            schema_key_for({
                'origin_did': issuer.did,
                'name': schema_def['name'],
                'version': schema_def['version']
            }))
        ledger_schema = json.loads(schema_json)

        self.__log_json('Schema:', ledger_schema)

        claim_def_json = await issuer.get_claim_def(
            ledger_schema['seqNo'], issuer.did)
        return (ledger_schema, claim_def_json)

    async def submit_claim(self, schema_name, schema_version, attribs):
        if not self.ready():
            raise RuntimeError('Issuer service is not ready')
        if not schema_name:
            raise ValueError('Missing schema name')
        if not attribs:
            raise ValueError('Missing request data')
        if not self._orgbook_did:
            raise ValueError('Missing DID for TOB')
        claim_type = self.find_claim_type_for_schema(schema_name, schema_version)
        if not claim_type:
            raise RuntimeError('Error locating claim type')

        claim = self.load_claim_request(claim_type, attribs)
        encoded_claim = encode_claim(claim)
        self.__log_json('Claim:', encoded_claim)

        von_client = self.init_von_client()
        tob_client = self.init_tob_client()

        async with von_client.create_issuer() as von_issuer:
            (ledger_schema, claim_def_json) = await self._create_issuer_claim_def(
                von_issuer, claim_type['schema'])

            # We create a claim offer
            schema_json = json.dumps(ledger_schema)
            logger.info('Creating claim offer for TOB at DID {}'.format(self._orgbook_did))
            claim_offer_json = await von_issuer.create_claim_offer(schema_json, self._orgbook_did)
            claim_offer = json.loads(claim_offer_json)

            self.__log_json('Requesting claim request:', {
                'claim_offer': claim_offer,
                'claim_def': json.loads(claim_def_json)
            })

            claim_req = tob_client.create_record('bcovrin/generate-claim-request', {
                'claim_offer': claim_offer_json,
                'claim_def': claim_def_json
            })
            self.__log_json('Got claim request:', claim_req)

            claim_request_json = json.dumps(claim_req)

            (_, claim_json) = await von_issuer.create_claim(
                claim_request_json, encoded_claim)

        self.__log_json('Created claim:', json.loads(claim_json))

        # Store claim
        return tob_client.create_record('bcovrin/store-claim', {
            'claim_type': ledger_schema['data']['name'],
            'claim_data': json.loads(claim_json)
        })

    def __log_json(self, heading, data):
        logger.debug(
            "\n============================================================================\n" +
            "{0}\n".format(heading) +
            "{0}\n".format(json.dumps(data, indent=2)) +
            "============================================================================\n")

