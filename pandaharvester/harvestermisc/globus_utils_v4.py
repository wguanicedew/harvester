"""
utilities routines associated with globus, for globus-sdk v4

differences from globus_utils.py:
- the Transfer Client can be created with an access token (AccessTokenAuthorizer)
- endpoint activation was removed from globus-sdk v4 (not needed for GCSv5 endpoints),
  so check_endpoint_activation only checks that the endpoint can be looked up
- TransferAPIError is a GlobusAPIError, so it is handled by the GlobusAPIError branch

"""
import inspect
import sys

from globus_sdk import (
    AccessTokenAuthorizer,
    GlobusAPIError,
    GlobusConnectionError,
    GlobusError,
    GlobusTimeoutError,
    NativeAppAuthClient,
    NetworkError,
    RefreshTokenAuthorizer,
    TransferClient,
)
from pandalogger.LogWrapper import LogWrapper

# handle exception from globus software


def handle_globus_exception(tmp_log):
    if not isinstance(tmp_log, LogWrapper):
        methodName = f"{inspect.stack()[1][3]} : "
    else:
        methodName = ""
    # extract errtype and check if it a GlobusError Class
    errtype, errvalue = sys.exc_info()[:2]
    errStat = None
    errMsg = f"{methodName} {errtype.__name__} "
    if isinstance(errvalue, GlobusAPIError):
        # Error response from the REST service, check the code and message for
        # details.
        errStat = None
        errMsg += f"HTTP status code: {errvalue.http_status} Error Code: {errvalue.code} Error Message: {errvalue.message} "
    elif isinstance(errvalue, GlobusConnectionError):
        errStat = None
        errMsg += "A connection error occured while making a REST request. "
    elif isinstance(errvalue, GlobusTimeoutError):
        errStat = None
        errMsg += "A REST request timeout. "
    elif isinstance(errvalue, NetworkError):
        errStat = None
        errMsg += "Network Failure. Possibly a firewall or connectivity issue "
    elif isinstance(errvalue, GlobusError):
        errStat = False
        errMsg += "Totally unexpected GlobusError! "
    else:  # some other error
        errStat = False
        errMsg = f"{errvalue} "
    tmp_log.error(errMsg)
    return (errStat, errMsg)


# Globus create transfer client with a refresh token


def create_globus_transfer_client(tmpLog, globus_client_id, globus_refresh_token):
    """
    create Globus Transfer Client with a refresh token and return the transfer client
    the access token is refreshed automatically by the authorizer
    """
    tmpLog.info("Creating instance of GlobusTransferClient with refresh token")
    tc = None
    errStat = True
    try:
        client = NativeAppAuthClient(client_id=globus_client_id)
        authorizer = RefreshTokenAuthorizer(refresh_token=globus_refresh_token, auth_client=client)
        tc = TransferClient(authorizer=authorizer)
    except BaseException:
        errStat = False
        handle_globus_exception(tmpLog)
    return errStat, tc


# Globus create transfer client with an access token


def create_globus_transfer_client_with_access_token(tmpLog, globus_access_token):
    """
    create Globus Transfer Client with a static access token and return the transfer client
    the access token is not refreshed, so a new client is needed once the token expires
    """
    tmpLog.info("Creating instance of GlobusTransferClient with access token")
    tc = None
    errStat = True
    try:
        authorizer = AccessTokenAuthorizer(globus_access_token)
        tc = TransferClient(authorizer=authorizer)
    except BaseException:
        errStat = False
        handle_globus_exception(tmpLog)
    return errStat, tc


def check_endpoint_activation(tmpLog, tc, endpoint_id):
    """
    check if endpoint is available
    endpoint activation doesn't exist in globus-sdk v4, so only check that the endpoint can be retrieved
    """
    # test we have a Globus Transfer Client
    if not tc:
        errStr = "failed to get Globus Transfer Client"
        tmpLog.error(errStr)
        return False, errStr
    try:
        endpoint = tc.get_endpoint(endpoint_id)
        errStr = f"Endpoint({endpoint_id}) - {endpoint['display_name']} - is available"
        tmpLog.debug(errStr)
        return True, errStr
    except BaseException:
        errStat, errMsg = handle_globus_exception(tmpLog)
        return errStat, {}


# get transfer tasks


def get_transfer_task_by_id(tmpLog, tc, transferID=None):
    # test we have a Globus Transfer Client
    if not tc:
        errStr = "failed to get Globus Transfer Client"
        tmpLog.error(errStr)
        return False, errStr
    if transferID is None:
        # error need to have task ID
        errStr = "failed to provide transfer task ID "
        tmpLog.error(errStr)
        return False, errStr
    try:
        # execute
        gRes = tc.get_task(transferID)
        # parse output
        tasks = {}
        tasks[transferID] = gRes
        # return
        tmpLog.debug(f"got {len(tasks)} tasks")
        return True, tasks
    except BaseException:
        errStat, errMsg = handle_globus_exception(tmpLog)
        return errStat, {}
