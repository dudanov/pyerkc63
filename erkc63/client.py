from __future__ import annotations

import asyncio
import datetime as dt
import functools
import logging
from typing import (
    Any,
    Awaitable,
    Callable,
    Concatenate,
    Coroutine,
    Iterable,
    Mapping,
    ParamSpec,
    Self,
    Sequence,
    TypeVar,
    overload,
)

import aiohttp
import yarl

from .account import AccountInfo, PublicAccountInfo
from .accrual import Accrual, AccrualDetalization, Accruals, MonthAccrual
from .bills import QrCodes
from .errors import (
    AccountBindingError,
    AccountNotFound,
    AuthorizationError,
    AuthorizationRequired,
    ParsingError,
)
from .meters import MeterInfoHistory, MeterValue, PublicMeterInfo
from .parsers import parse_account, parse_accounts, parse_meters, parse_token
from .payment import Payment
from .utils import (
    data_attr,
    date_attr,
    date_last_accrual,
    date_to_str,
    str_normalize,
    str_to_date,
    to_float,
)

_LOGGER = logging.getLogger(__name__)

_SEMAPHORE = asyncio.Semaphore()
"""Глобальный семафор выполнения ограничения сервера одной сессии на IP"""

_MIN_DATE = dt.date(2018, 1, 1)
_MAX_DATE = dt.date(2099, 12, 31)

APP_URL = yarl.URL("https://lk.erkc63.ru")

P = ParamSpec("P")
T = TypeVar("T")

ClientMethod = Callable[Concatenate["Self@ErkcClient", P], Awaitable[T]]


@overload
def api(func: ClientMethod, /) -> ClientMethod: ...


@overload
def api(
    *,
    auth_required: bool = True,
    check_only: bool = False,
    public: bool = False,
) -> Callable[[ClientMethod], ClientMethod]: ...


def api(
    func: ClientMethod | None = None,
    /,
    *,
    auth_required: bool = True,
    check_only: bool = False,
    public: bool = False,
) -> ClientMethod | Callable[[ClientMethod], ClientMethod]:
    """Декоратор методов API клиента"""

    def decorator(func: ClientMethod):
        @functools.wraps(func)
        async def _wrapper(self: "ErkcClient", *args, **kwargs):
            nonlocal auth_required

            self._check_session()

            if check_only:
                return await func(self, *args, **kwargs)

            if public:
                auth_required = False
                await self.close(close_transport=False)

            if not self.opened:
                await self.open(auth=auth_required)

            if auth_required and not self.authorized:
                await self.open()

            return await func(self, *args, **kwargs)

        return _wrapper

    if func is None:
        return decorator

    return decorator(func)


class ErkcClient:
    """
    Клиент личного кабинета ЕРКЦ.
    """

    _cli: aiohttp.ClientSession
    """Клиентская сессия."""
    _login: str | None
    """Логин (адрес электронной почты)."""
    _password: str | None
    """Пароль."""
    _token: str | None
    """Токен сессии."""
    _accounts: tuple[int, ...] | None
    """Лицевые счета, привязанные к аккаунту."""

    def __init__(
        self,
        login: str | None = None,
        password: str | None = None,
        *,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        """
        Создает клиент личного кабинета ЕРКЦ.

        Параметры:
        - `login`: логин (электронная почта).
        - `password`: пароль.
        - `session`: готовый объект `aiohttp.ClientSession`.
        если он не указан в параметре `session`.
        """

        self._cli = session or aiohttp.ClientSession(base_url=APP_URL)
        self._login = login
        self._password = password
        self._accounts = None
        self._token = None

    async def __aenter__(self):
        try:
            await self.open()

        except Exception:
            await self.close()
            raise

        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        await self.close()

    def _check_session(self) -> None:
        """
        Выполняет проверку времени жизни сессии.
        Сбрасывает при ее истечении либо отсутствии куки.
        """

        for cookie in self._cli.cookie_jar:
            if cookie.key == "laravel_session":
                expires = cookie["expires"]
                expires = dt.datetime.strptime(expires, "%a, %d-%b-%Y %H:%M:%S %Z")
                expires = expires.replace(tzinfo=dt.UTC)

                if dt.datetime.now(dt.UTC).replace(microsecond=0) >= expires:
                    break

                return

        self._reset()

    def _post(self, path: str, **data: Any):
        data["_token"] = self._token
        return self._cli.post(path, data=data)

    def _get(self, path: str, **params: Any):
        return self._cli.get(path, params=params)

    async def _ajax(self, func: str, account: int | None, **params: Any) -> Any:
        async with self._get(f"/ajax/{self._account(account)}/{func}", **params) as x:
            return await x.json()

    def _history(
        self, what: str, account: int | None, start: dt.date, end: dt.date
    ) -> Coroutine[Any, Any, Sequence[Sequence[str]]]:
        params = {"from": date_to_str(start), "to": date_to_str(end)}

        return self._ajax(f"{what}History", account, **params)

    def _update_accounts(self, html: str):
        self._accounts = parse_accounts(html)
        _LOGGER.debug(f"Привязанные к личному кабинету лицевые счета: {self._accounts}")

    @property
    def closed(self) -> bool:
        """Коннектор клиента закрыт."""

        return self._cli.closed

    @property
    def opened(self) -> bool:
        """Сессия открыта."""

        return not (self.closed or self._token is None)

    @property
    def authorized(self) -> bool:
        """Авторизация в аккаунте выполнена."""

        return not (self.closed or self._accounts is None)

    @property
    def accounts(self) -> tuple[int, ...]:
        """Лицевые счета, привязанные к аккаунту личного кабиента."""

        if self._accounts is None:
            raise AuthorizationRequired("Требуется авторизация")

        return self._accounts

    @property
    def account(self) -> int:
        """Основной лицевой счет личного кабинета."""

        if x := self.accounts:
            return x[0]

        raise AccountNotFound("Основной лицевой счет не привязан")

    def _account(self, account: int | None) -> int:
        if account is None:
            return self.account

        if account in self.accounts:
            return account

        raise AccountNotFound("Лицевой счет %d не привязан", account)

    @api(check_only=True)
    async def open(
        self,
        login: str | None = None,
        password: str | None = None,
        auth: bool = True,
    ) -> None:
        """Открытие сессии"""

        if not self.opened:
            await _SEMAPHORE.acquire()

            _LOGGER.debug("Открытие новой сессии")

            async with self._get("/login") as x:
                html = await x.text()

            self._token = parse_token(html)

            _LOGGER.debug("Сессия открыта. Токен: %s", self._token)

        if not auth or self.authorized:
            return

        login, password = login or self._login, password or self._password

        if not (login and password):
            raise AuthorizationError("Не заданы параметры входа")

        _LOGGER.debug("Вход в аккаунт %s", login)

        async with self._post("/login", login=login, password=password) as x:
            if x.url == x.history[0].url:
                raise AuthorizationError("Ошибка входа. Проверьте логин и пароль")

            _LOGGER.debug("Вход в аккаунт %s успешно выполнен", login)

            html = await x.text()

        self._update_accounts(html)

        # Сохраняем актуальную пару логин-пароль
        self._login, self._password = login, password

    @api(check_only=True)
    async def close(self, close_transport: bool = True) -> None:
        """Выход из аккаунта личного кабинета и закрытие сессии."""

        try:
            if self.authorized:
                _LOGGER.debug("Выход из аккаунта %s", self._login)

                async with self._get("/logout") as x:
                    await x.text()

                self._reset()

        finally:
            if close_transport:
                await self._cli.close()

            _SEMAPHORE.release()

    def _reset(self):
        self._token = None
        self._accounts = None

    @api
    async def download_pdf(self, accrual: Accrual, peni: bool = False) -> bytes:
        """
        Загружает квитанцию в формате PDF. При неудаче возвращает пустые данные.

        Параметры:
        - `accrual`: квитанция.
        - `peni`: нужна квитанция пени.
        """

        if not (id := accrual.peni_id if peni else accrual.bill_id):
            return b""

        try:
            json = await self._ajax("getReceipt", accrual.account, receiptId=id)

        except Exception:
            return b""

        async with self._get(json["file"]) as x:
            return await x.read()

    @api
    async def qr_codes(self, accrual: Accrual) -> QrCodes:
        """
        Загружает PDF квитанции и извлекает QR коды оплаты.
        Возвращает объект `QrCodes`.

        Параметры:
        - `accrual`: квитанция.
        """

        result = await asyncio.gather(
            self.download_pdf(accrual, False),
            self.download_pdf(accrual, True),
        )

        return QrCodes(*result)

    @api
    async def year_accruals(
        self,
        year: int | None = None,
        *,
        account: int | None = None,
        limit: int | None = None,
        include_details: bool = False,
    ) -> tuple[Accrual, ...]:
        """
        Запрос квитанций лицевого счета за год.

        Если год не уточняется - используется текущий.

        Параметры:
        - `year`: год.
        - `account`: номер лицевого счета. Если `None` - будет использоваться
        основной лицевой счет личного кабинета.
        - `limit`: кол-во последних квитанций в ответе. По-умолчанию все квитанции за год.
        - `include_details`: дополнительный запрос детализированных затрат на каждую
        квитанцию в полученном результате. По-умолчанию: `False`.
        """

        account = self._account(account)

        resp: Sequence[Sequence[str]] = await self._ajax(
            "getReceipts", account, year=year or date_last_accrual().year
        )

        db: dict[dt.date, Accrual] = {}

        for data in resp:
            date = date_attr(data[0])

            if limit and limit == len(db) and date not in db:
                break

            record = db.setdefault(
                date,
                Accrual(
                    account=account,
                    date=date,
                    summa=to_float(data[1]),
                    peni=to_float(data[2]),
                ),
            )

            id = data_attr(data[5])

            match data[3]:
                case "общая":
                    record.bill_id = id
                case "пени":
                    record.peni_id = id
                case _:
                    raise ParsingError

        result = tuple(db.values())

        if include_details:
            await self.update_accruals(result)

        return result

    @api
    async def update_accrual(self, accrual: Accruals) -> None:
        """
        Обновление детализированных данных квитанции или начисления.

        Параметры:
        - `accrual`: квитанция/начисление для обновления.
        """

        resp: list[list[str]] = await self._ajax(
            "accrualsDetalization",
            accrual.account,
            month=accrual.date.strftime("01.%m.%y"),
        )

        accrual.details = {
            str_normalize(x[0]): AccrualDetalization(*map(to_float, x[1:]))
            for x in resp
        }

    @api
    def update_accruals(self, accruals: Iterable[Accruals]):
        """
        Обновление детализированных данных квитанций или начислений.

        Параметры:
        - `accruals`: квитанции/начисления для обновления.
        """

        return asyncio.gather(*map(self.update_accrual, accruals))

    @api
    async def meters_history(
        self,
        *,
        start: dt.date | None = None,
        end: dt.date | None = None,
        account: int | None = None,
    ) -> tuple[MeterInfoHistory, ...]:
        """
        Запрос счетчиков лицевого счета с историей показаний.

        Если даты не уточняются - результат будет включать все доступные показания.

        Параметры:
        - `start`: дата начала периода.
        - `end`: дата окончания периода (включается в ответ).
        - `account`: номер лицевого счета. Если `None` - будет использоваться
        основной лицевой счет личного кабинета.
        """

        start, end = start or _MIN_DATE, end or _MAX_DATE

        assert start <= end

        db: dict[tuple[str, str], list[MeterValue]] = {}

        while True:
            history = await self._history("counters", account, start, end)

            # Лимит записей ответа сервера - 25. Контроль превышения на случай изменения API.
            assert (num := len(history)) <= 25

            # Множество для проверки содержания в ответе данных от одной даты.
            unique_dates = set()

            for _, key, date, value, consumption, source in history:
                unique_dates.add(end := str_to_date(date[27:35]))

                # игнорируем записи без потребления
                if not (consumption := float(consumption)):
                    continue

                value = MeterValue(end, float(value), consumption, source)

                name, serial = key.split(", счетчик №", 1)
                db.setdefault((name, serial), []).append(value)

            if num < 25:
                break

            # Возможен баг: если в один день число записей больше лимита,
            # то сервер не сможет вернуть полный результат ни при каких условиях.
            # Этот случай крайне маловероятен, но выполнена проверка и обход ситуации.
            if len(unique_dates) == 1:
                _LOGGER.warning("Результат может содержать неполные данные.")

                if start == end:
                    break

                end -= dt.timedelta(days=1)
                _LOGGER.warning("Применен обход.")

        # Исключаем дублирование записей из наложенных ответов и конвертируем в кортеж
        return tuple(
            MeterInfoHistory(*k, tuple(dict.fromkeys(v))) for k, v in db.items()
        )

    @api
    async def accruals_history(
        self,
        *,
        start: dt.date | None = None,
        end: dt.date | None = None,
        account: int | None = None,
        include_details: bool = False,
    ) -> tuple[MonthAccrual, ...]:
        """
        Запрос начислений за заданный период.

        Если даты не уточняются - результат будет включать все доступные показания.

        Параметры:
        - `start`: дата начала периода.
        - `end`: дата окончания периода (включается в ответ).
        - `account`: номер лицевого счета. Если `None` - будет использоваться
        основной лицевой счет личного кабинета.
        - `include_details`: дополнительный запрос детализированных затрат на каждое
        начисление в полученном результате. По-умолчанию: `False`.
        """

        account = self._account(account)
        start, end = start or _MIN_DATE, end or _MAX_DATE

        assert start <= end

        resp = await self._history("accruals", account, start, end)

        result = []

        for date, *floats in resp:
            floats: Any = map(to_float, floats)
            accrual = MonthAccrual(account, date_attr(date), *floats)

            # запрос поломан. возвращает нулевые начисления в невалидном диапазоне дат.
            # при первом нулевом начислении прерываем цикл, так как далее все начисления тоже нулевые.
            if not accrual.summa:
                break

            result.append(accrual)

        if include_details:
            await self.update_accruals(result)

        return tuple(result)

    @api
    async def payments_history(
        self,
        *,
        start: dt.date | None = None,
        end: dt.date | None = None,
        account: int | None = None,
    ) -> tuple[Payment, ...]:
        """
        Запрос истории платежей за заданный период.

        Если даты не уточняются - результат будет включать все доступные показания.

        Параметры:
        - `start`: дата начала периода.
        - `end`: дата окончания периода (включается в ответ).
        - `account`: номер лицевого счета. Если `None` - будет использоваться
        основной лицевой счет личного кабинета.
        """

        start, end = start or _MIN_DATE, end or _MAX_DATE

        assert start <= end

        resp = await self._history("payments", account, start, end)
        result = (Payment(date_attr(x), to_float(y), z) for x, y, z in resp)

        # Ответ содержит нулевые платежи (внутренние перерасчеты). Применим фильтр.
        return tuple(x for x in result if x.summa)

    @api
    async def account_info(self, account: int | None = None) -> AccountInfo:
        """
        Запрос информации о лицевом счете.

        Параметры:
        - `account`: номер лицевого счета. Если `None` - будет использоваться
        основной лицевой счет личного кабинета.
        """

        account = self._account(account)

        async with self._get(f"/account/{account}") as x:
            html = await x.text()

        return parse_account(html)

    @api
    async def account_add(
        self,
        account: int | PublicAccountInfo,
        last_bill_amount: float = 0,
    ) -> None:
        """
        Привязка лицевого счета к аккаунту личного кабинета.

        Параметры:
        - `account`: номер или публичная информация о лицевом счете
        - `last_bill_amount`: сумма последнего начисления.
        Может быть взята автоматически из публичной информации о счете.
        """

        if isinstance(account, PublicAccountInfo):
            last_bill_amount = last_bill_amount or account.balance
            account = account.account

        if account in self.accounts:
            _LOGGER.info("Лицевой счет %d уже привязан к аккаунту", account)
            return

        if last_bill_amount <= 0:
            raise ValueError("Сумма последнего начисления не указана")

        _LOGGER.debug("Привязка лицевого счета %d", account)

        async with self._post(
            "/account/add", account=account, summ=last_bill_amount
        ) as x:
            html = await x.text()

        self._update_accounts(html)

        if account not in self.accounts:
            raise AccountBindingError("Не удалось привязать лицевой счет %d", account)

    @api
    async def account_rm(self, account: int) -> None:
        """
        Отвязка лицевого счета от аккаунта личного кабинета.

        Параметры:
        - `account`: номер лицевого счета.
        """

        if account not in self.accounts:
            _LOGGER.info("Лицевой счет %d не привязан к аккаунту", account)
            return

        async with self._post(f"/account/{account}/remove") as x:
            html = await x.text()

        self._update_accounts(html)

        if account in self.accounts:
            raise AccountBindingError("Не удалось отвязать лицевой счет %d", account)

    async def _set_meters_values(
        self,
        path: str,
        values: Mapping[int, float],
    ) -> None:
        if not values:
            return

        async with self._get(path) as x:
            html = await x.text()

        data: dict[str, Any] = {}
        meters = parse_meters(html)

        # Если используем без авторизации - извлечем номер лицевого счета
        # из пути запроса и добавим в данные запроса
        if not path.startswith("/account"):
            data["ls"] = int(path.rsplit("/", 1)[-1])

        for id, value in values.items():
            if m := meters.get(id):
                if value > m.value:
                    data[f"counters[{id}_0][value]"] = value
                    data[f"counters[{id}_0][rawId]"] = id
                    data[f"counters[{id}_0][tarif]"] = 0

                    continue

                raise ValueError(
                    f"Новое значение счетчика {id} должно быть выше текущего {m.value}"
                )

            raise ValueError(f"Счетчик {id} не найден")

        async with self._post(path, **data):
            pass

    @api
    async def meters_info(
        self, account: int | None = None
    ) -> Mapping[int, PublicMeterInfo]:
        """
        Запрос информации о приборах учета по лицевому счету.

        Возвращает словарь `идентификатор - информация о приборе учета`.

        Включает следующую информацию:
        - Внутренний идентификатор (для отправки новых показаний)
        - Серийный номер
        - Дата последнего показания
        - Последнее показание
        """

        async with self._get(f"/account/{self._account(account)}/counters") as x:
            html = await x.text()

        return parse_meters(html)

    @api(public=True)
    async def pub_meters_info(self, account: int) -> Mapping[int, PublicMeterInfo]:
        """
        Запрос публичной информации о приборах учета по лицевому счету.

        Возвращает словарь `идентификатор - информация о приборе учета`.

        Включает следующую информацию:
        - Внутренний идентификатор (для отправки новых показаний)
        - Серийный номер
        - Дата последнего показания
        - Последнее показание

        Параметры:
        - `account`: номер лицевого счета.
        """

        async with self._get(f"/counters/{account}") as x:
            html = await x.text()

        return parse_meters(html)

    @api(public=True)
    async def pub_set_meters_values(
        self,
        account: int,
        values: Mapping[int, float],
    ) -> None:
        """
        Передача новых показаний приборов учета без авторизации.

        Параметры:
        - `account`: номер лицевого счета.
        - `values`: словарь `идентификатор прибора - новое показание`.
        """

        await self._set_meters_values(f"/counters/{account}", values)

    @api(public=True)
    async def pub_account_info(self, account: int) -> PublicAccountInfo | None:
        """
        Запрос открытой информации по лицевому счету.

        Параметры:
        - `account`: номер лицевого счета.
        """

        async with self._get("/payment/checkLS", ls=account) as x:
            json: Mapping[str, Any] = await x.json()

        if json["checkLS"]:
            return PublicAccountInfo(
                account,
                str_normalize(json["address"]),
                to_float(json["balanceSumma"]),
                to_float(json["balancePeni"]),
            )

        _LOGGER.info("Лицевой счет %d не найден", account)

    @api(public=True)
    async def pub_accounts_info(
        self, *accounts: int
    ) -> Mapping[int, PublicAccountInfo]:
        """
        Запрос открытой информации по лицевым счетам.

        Параметры:
        - `accounts`: номера лицевых счетов.
        """

        result = await asyncio.gather(*map(self.pub_account_info, accounts))

        return {x.account: x for x in result if x}
