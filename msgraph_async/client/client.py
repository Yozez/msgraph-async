import typing
import logging
import json
import aiohttp
from msgraph_async.common.constants import *
from msgraph_async.common.exceptions import *
from msgraph_async.common.odata_query import *
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from functools import wraps


def authorized(func):
    @wraps(func)
    def actual(*args, **kwargs):
        client: GraphClient = args[0]
        if not client._token and not kwargs.get("token"):
            raise Exception('Token is not managed so you must explicitly provide it')

        token = kwargs["token"] if kwargs.get("token") else client._token
        kwargs["_req_headers"]: dict = client._build_auth_header(token)
        kwargs["_req_headers"].update({"Content-type": "application/json"})
        if kwargs.get("extra_headers"):
            kwargs["_req_headers"].update(kwargs["extra_headers"])
        return func(*args, **kwargs)

    return actual


class GraphClient:

    def __init__(self, enable_logging=False):
        self._session = aiohttp.ClientSession()
        self._token = None
        self._managed = False
        self._token_refresh_interval_sec = 3300
        self._scheduler = AsyncIOScheduler()
        self._enable_logging = enable_logging

    @property
    def token_refresh_interval_sec(self):
        return self._token_refresh_interval_sec

    @token_refresh_interval_sec.setter
    def token_refresh_interval_sec(self, value):
        if value < 60 or value > 3600:
            raise ValueError("refresh interval must be between 60 and 3600 seconds")
        self._token_refresh_interval_sec = value

    @property
    def is_managed(self):
        return self._managed

    @property
    def token(self):
        return self._token

    @staticmethod
    def _build_url(version, resources: typing.List[typing.Tuple], **kwargs):
        # url = GRAPH_BASE_URL + version + resource
        url = GRAPH_BASE_URL + version
        for resource, resource_id in resources:
            url += resource
            if resource_id:
                url += f"/{resource_id}"

        odata_query: ODataQuery = kwargs.get("odata_query")
        if odata_query:
            url += str(odata_query)
        return url

    @staticmethod
    def _build_auth_header(token: str):
        if token.lower().startswith("bearer"):
            return {"authorization": token}
        else:
            return {"authorization": f"bearer {token}"}

    @staticmethod
    def _get_resource(resource_template: SubscriptionResourcesTemplates, resource_id):
        return resource_template.value.format(resource_id)

    @staticmethod
    def _get_microsoft_time_format(minutes_to_expiration: int):
        val = (datetime.utcnow() + timedelta(minutes=minutes_to_expiration)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "0Z"
        return val

    def _log(self, level, msg):
        if self._enable_logging:
            logging.log(level, msg)

    async def _refresh_token(self, app_id, app_secret, tenant_id):
        content, status_code = await self.acquire_token(app_id, app_secret, tenant_id)
        self._token = content['access_token']
        self._log(logging.INFO, "token has been refreshed")

    async def _request(self, method, url, headers: dict = None, data: dict = None, expected_statuses=None):
        if not expected_statuses:
            expected_statuses = (HTTPStatus.OK, HTTPStatus.NO_CONTENT, HTTPStatus.CREATED)
        async with self._session.request(method, url, headers=headers, data=data) as resp:
            status = resp.status
            resp_headers = resp.headers
            if 'application/json' in resp.headers['Content-Type']:
                r: dict = await resp.json()
            else:
                r: bytes = await resp.read()

        if status in expected_statuses:
            return r, status
        else:
            raise status2exception.get(status, UnknownError)(r, resp_headers)

    async def acquire_token(self, app_id, app_secret, tenant_id):
        """
        Get token from Microsoft
        :param app_id: Also called client id, the identifier of your application in Azure,
        consent must have been granted in order to success
        :param app_secret: Secret of your application in Azure
        :param tenant_id: Id of the tenant you're asking access to its' resources
        :return: Dictionary with token data
        """
        req_body = {
            "grant_type": "client_credentials",
            "scope": "https://graph.microsoft.com/.default",
            "client_id": app_id,
            "client_secret": app_secret,
        }
        url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
        content, status = await self._request("POST", url, data=req_body)
        return content, status

    async def manage_token(self, app_id, app_secret, tenant_id):
        """
        Let the client keep a valid token for you.
        The client will refresh it (i.e. acquire new one) every token_refresh_interval_sec.
        If the token is managed, then you don't need to provide it in every request.
        :param app_id: app_id: Also called client id, the identifier of your application in Azure,
        consent must have been granted in order to success
        :param app_secret: Secret of your application in Azure
        :param tenant_id: Id of the tenant you're asking access to its' resources
        :return: None if the operation was successful, else - raises GraphClientException with info
        """
        if self._managed:
            raise GraphClientException("Token is already managed")
        try:
            await self._refresh_token(app_id, app_secret, tenant_id)
            self._scheduler.add_job(self._refresh_token, 'interval', args=[app_id, app_secret, tenant_id],
                                    seconds=self._token_refresh_interval_sec)
            self._scheduler.start()
            self._managed = True
        except Exception as e:
            self._log(logging.ERROR, f"exception while trying to set manage token: {str(e)}")
            raise GraphClientException(e)

    @authorized
    async def get_user(self, user_id, **kwargs):
        """
        Get user by its' ID
        :param user_id: thd ID of the user
        :param
        :return:
        """
        url = self._build_url(V1_EP, [(USERS, user_id)], **kwargs)
        res, status = await self._request(
            "GET", url, kwargs["_req_headers"], expected_statuses=kwargs.get("expected_statuses"))
        return res, status

    @authorized
    async def list_users_bulk(self, **kwargs):
        url = self._build_url(V1_EP, [(USERS, None)], **kwargs)
        res, status = await self._request("GET", url, kwargs["_req_headers"],
                                          expected_statuses=kwargs.get("expected_statuses"))
        return res, status

    @authorized
    async def list_more_users(self, next_url, **kwargs):
        res, status = await self._request("GET", next_url, kwargs["_req_headers"],
                                          expected_statuses=kwargs.get("expected_statuses"))
        return res, status

    @authorized
    async def list_all_users(self, **kwargs):
        res, status = await self.list_users_bulk(**kwargs)
        next_url = res.get(NEXT_KEY)
        for user in res["value"]:
            yield user
        while next_url:
            res, status = await self.list_more_users(next_url, **kwargs)
            next_url = res.get(NEXT_KEY)
            for user in res["value"]:
                yield user

    @authorized
    async def create_subscription(self, change_type, notification_url, resource, minutes_to_expiration,
                                  client_state=None, latest_supported_tls_version=None, user_id=None, **kwargs):
        if type(resource) == SubscriptionResourcesTemplates:
            if not user_id:
                raise GraphClientException(f"user id must be specified with resource template ({resource.name})")
            resource = self._get_resource(resource, user_id)

        expiration_date_time = self._get_microsoft_time_format(minutes_to_expiration)

        url = self._build_url(V1_EP, [(SUBSCRIPTIONS, None)])
        body = {
            "changeType": change_type,
            "notificationUrl": notification_url,
            "resource": resource,
            "expirationDateTime": expiration_date_time,
            "clientState": "secretClientValue"
        }
        if client_state:
            body["clientState"] = client_state

        if latest_supported_tls_version:
            body["latestSupportedTlsVersion"] = latest_supported_tls_version

        res, status = await self._request("POST", url, kwargs["_req_headers"], json.dumps(body), kwargs.get("expected_statuses"))
        return res, status

    @authorized
    async def renew_subscription(self, subscription_id: str, minutes_to_expiration: int, **kwargs):
        url = self._build_url(V1_EP, [(SUBSCRIPTIONS, subscription_id)])
        expiration_date_time = self._get_microsoft_time_format(minutes_to_expiration)
        body = {
            "expirationDateTime": expiration_date_time
        }

        res, status = await self._request("PATCH", url, kwargs["_req_headers"], json.dumps(body),
                                          kwargs.get("expected_statuses"))
        return res, status

    @authorized
    async def delete_subscription(self, subscription_id: str, **kwargs):
        url = self._build_url(V1_EP, [(SUBSCRIPTIONS, subscription_id)])
        res, status = await self._request(
            "DELETE", url, kwargs["_req_headers"], expected_statuses=kwargs.get("expected_statuses"))
        return res, status

    @authorized
    async def list_user_mails_bulk(self, user_id, **kwargs):
        url = self._build_url(V1_EP, [(USERS, user_id), (MAILS, None)], **kwargs)
        res, status = await self._request("GET", url, kwargs["_req_headers"],
                                          expected_statuses=kwargs.get("expected_statuses"))
        return res, status

    @authorized
    async def list_more_user_mails(self, next_url, **kwargs):
        res, status = await self._request("GET", next_url, kwargs["_req_headers"],
                                          expected_statuses=kwargs.get("expected_statuses"))
        return res, status

    @authorized
    async def list_all_user_mails(self, user_id, **kwargs):
        res, status = await self.list_user_mails_bulk(user_id, **kwargs)
        next_url = res.get(NEXT_KEY)
        for mail in res["value"]:
            yield mail
        while next_url:
            res, status = await self.list_more_user_mails(next_url, **kwargs)
            next_url = res.get(NEXT_KEY)
            for mail in res["value"]:
                yield mail

    @authorized
    async def get_mail(self):
        pass

    @authorized
    async def move_mail(self):
        pass
