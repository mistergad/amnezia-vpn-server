# Amnezia Service

Рабочий MVP сервиса подписки на VPN: принимает оплату, активирует подписку только после проверенного webhook, создает отдельную конфигурацию AmneziaWG для каждого устройства и дает оператору веб-интерфейс управления клиентами.

## Что уже реализовано

- регистрация и вход, подписанные HttpOnly-сессии и CSRF-защита форм;
- единый баланс: 100 ₽ дают 30 дней одному устройству;
- автоматический расчет расхода по числу активных ключей;
- тестовый платежный шлюз и реальный адаптер ЮKassa;
- повторная серверная проверка объекта платежа, суммы, валюты и внутреннего ID;
- автоматическая выдача первого `.conf`, QR и гостевого текстового `vpn://`-ключа после оплаты;
- шифрование конфигураций в базе с Fernet;
- отдельный peer и IP для каждого устройства, отзыв доступа без перезапуска сервера;
- админка: клиент, устройство, IP, handshake, трафик, статус и отзыв;
- временная приостановка ключей при нулевом балансе и автоматическое
  восстановление тех же ключей после пополнения;
- PostgreSQL для production, SQLite по умолчанию для локального запуска;
- тесты полного сценария «регистрация → оплата → ключ → отзыв».

Официальное приложение принимает конфигурации `.conf`; это безопаснее полного `vpn://`-ключа self-hosted сервера, который дает административный доступ. См. [форматы конфигураций Amnezia](https://docs.amnezia.org/ru/documentation/supported-configuration-formats/) и [разницу между полным и гостевым доступом](https://docs.amnezia.org/documentation/instructions/share-connection/).

## Как считается баланс

Баланс хранится как оплаченное «устройство-время», поэтому добавление и отзыв ключей не вызывают ошибок округления:

- 100 ₽ и 1 устройство — 30 дней;
- 100 ₽ и 2 устройства — 15 дней;
- 300 ₽ и 3 устройства — 30 дней;
- добавление ключа сразу увеличивает расход и сокращает прогнозируемый срок;
- отзыв ключа сразу уменьшает расход; если активных ключей нет, баланс приостанавливается.

Расход учитывается посекундно. Когда баланс заканчивается, фоновая сверка
временно удаляет peer-ключи из AWG2, но сохраняет их конфигурации, IP и статус
приостановки. После следующего успешного пополнения те же ключи автоматически
возвращаются в AWG2 и снова работают без повторного импорта на устройствах.
Ручной отзыв или удаление устройства по-прежнему безвозвратны. Старые
оплаченные периоды при обновлении автоматически конвертируются в баланс без
потери оставшегося времени.

## Быстрый локальный запуск

Нужен Python 3.12+.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env             # Windows: copy .env.example .env
```

Для локального запуска оставьте `PAYMENT_PROVIDER=mock` и `VPN_BACKEND=mock`, затем:

```bash
uvicorn app.main:app --reload
```

Откройте `http://localhost:8000`. Тестовый платеж ничего не списывает. Администратор создается из `ADMIN_EMAIL` и `ADMIN_PASSWORD` при первом старте.

Проверка:

```bash
pytest
```

## Запуск control plane через Docker

```bash
cp .env.example .env
# Измените пароли, SECRET_KEY, BASE_URL и TRUSTED_HOSTS
docker compose up -d --build
```

Контейнерный вариант по умолчанию использует безопасный `mock`-узел. Для production не монтируйте Docker socket в веб-контейнер: это фактически root-доступ к хосту. Рекомендуемый вариант — запустить control plane через systemd на VPN-хосте либо вынести сетевой узел на отдельную машину с узким API/SSH-контуром.

## Автоматический deploy на VPS

Для чистого выделенного VPS с Ubuntu 24.04 достаточно заранее:

1. Если используется домен, направить его A-запись на публичный IPv4 сервера.
2. В firewall/security group провайдера открыть TCP `22`, `80`, `443` и UDP `55424`.
3. Скопировать репозиторий на VPS и запустить. Публичный IPv4 автоматически
   определяется на интерфейсе `eth0`:

   ```bash
   git clone <URL-вашего-репозитория> amnezia-service
   cd amnezia-service
   sudo bash deploy/vps-bootstrap.sh \
     --admin-email admin@example.com
   ```

   Если публичный интерфейс называется иначе, укажите его через
   `--interface ens3`. Домен или конкретный адрес можно явно задать через
   `--host`; это нужно при NAT, нескольких IP или использовании домена.

Пароль администратора генерируется автоматически. После завершения URL и пароль находятся в `/root/amnezia-service-credentials.txt` с правами `0600`. При необходимости пароль и UDP-порт можно передать явно:

```bash
sudo bash deploy/vps-bootstrap.sh \
  --host vpn.example.com \
  --admin-email admin@example.com \
  --admin-password 'Strong-Password-2026!' \
  --awg-port 55424
```

Если автоматическое определение невозможно из-за NAT или нескольких адресов,
передайте публичный IPv4 VPS явно:

```bash
sudo bash deploy/vps-bootstrap.sh \
  --host 203.0.113.10 \
  --admin-email admin@localhost
```

При публичном IPv4 на `eth0` достаточно просто `sudo bash
deploy/vps-bootstrap.sh`: явное указание IP не требуется.

В режиме публичного IP Caddy запрашивает у Let's Encrypt публично доверенный
короткоживущий IP-сертификат и автоматически его обновляет. Для выпуска и
обновления сертификата TCP-порты `80` и `443` должны быть доступны из
Интернета. При переходе с резервного локального сертификата deploy-скрипт
не удаляет его безвозвратно: он переносит старый сертификат в закрытый архив
`/var/lib/caddy/.local/share/caddy/migrated-local-certificates`, перезапускает
Caddy и запрашивает публичный сертификат. Если провайдер или firewall не
позволяет пройти проверку, можно явно включить резервный локальный сертификат:

```bash
sudo bash deploy/vps-bootstrap.sh --ip-tls-mode internal
```

В резервном режиме браузер покажет предупреждение, пока корневой сертификат
`/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt` не импортирован
в доверенные центры сертификации администраторского устройства.

Bootstrap автоматически устанавливает Docker, PostgreSQL, Caddy и systemd-службу, собирает официальный контейнер AWG2, создает БД, секреты и HTTPS-конфигурацию. Скрипт можно запускать повторно: существующий `amnezia-awg2` не пересоздается, его peer-ключи сохраняются, а панель учитывает уже занятые на живом интерфейсе IP.

Серверные скрипты AWG2 перенесены из `amnezia-vpn/amnezia-client` и зафиксированы на commit `06d219b92bfa7e7e8c43cca6e72e354d304b42a7`; происхождение и GPL-3.0 лицензия описаны в `deploy/vendor/amnezia-client/UPSTREAM.md`. Это убирает интерактивную установку через десктопный клиент и не позволяет будущему изменению репозитория незаметно поменять deploy.

По умолчанию платежи остаются тестовыми (`PAYMENT_PROVIDER=mock`). Для ЮKassa заполните `PAYMENT_PROVIDER`, `YOOKASSA_SHOP_ID` и `YOOKASSA_SECRET_KEY` в `/etc/amnezia-service.env`, затем выполните:

```bash
sudo systemctl restart amnezia-service
```

Полезные команды после установки:

```bash
sudo systemctl status amnezia-service caddy
sudo journalctl -u amnezia-service -f
sudo docker exec amnezia-awg2 awg show awg0
sudo docker exec amnezia-awg2 cat /opt/amnezia/awg/awg0.conf
```

Для резервной копии нужны PostgreSQL, `/etc/amnezia-service.env` (особенно `ENCRYPTION_KEY`) и `/opt/amnezia/awg/awg0.conf` из контейнера. HTTPS-сертификат Caddy получает и обновляет автоматически: для домена DNS должен указывать на VPS, а для домена или публичного IP TCP `80/443` должны быть доступны извне.

## Подключение существующего AmneziaWG

1. Установите AmneziaWG на VPS штатным приложением AmneziaVPN и проверьте обычное подключение вручную.
2. Найдите имя интерфейса и бинарники внутри контейнера:

   ```bash
   sudo docker exec amnezia-awg2 awg show
   sudo docker exec amnezia-awg2 sh -lc 'command -v awg; command -v awg-quick'
   ```

3. Создайте пользователя `amnezia-service`, установите приложение в `/opt/amnezia-service` и используйте unit из `deploy/amnezia-service.service`.
4. Адаптируйте `deploy/amnezia-service.sudoers.example` под реальные абсолютные пути. Проверьте файл через `visudo -cf` перед установкой.
5. В `/etc/amnezia-service.env` задайте:

   ```dotenv
   ENVIRONMENT=production
   BASE_URL=https://vpn.example.com
   SECRET_KEY=<случайная строка не короче 32 символов>
   ENCRYPTION_KEY=<отдельный Fernet-ключ>
   SESSION_HTTPS_ONLY=true
   TRUSTED_HOSTS=["vpn.example.com"]
   DATABASE_URL=postgresql+psycopg://...

   VPN_BACKEND=native
   AWG_INTERFACE=awg0
   AWG_ENDPOINT=<публичный-ip-или-домен>:<udp-порт>
   AWG_SUBNET=10.8.1.0/24
   AWG_COMMAND_PREFIX=["sudo","-n","docker","exec","-i","amnezia-awg2"]
   AWG_BINARY=/usr/bin/awg
   AWG_QUICK_BINARY=/usr/bin/awg-quick
   AWG_CONFIG_PATH=/opt/amnezia/awg/awg0.conf
   AWG_SAVE_CONFIG=true
   ```

   Если `AWG_CONFIG_PATH` не существует на хосте, сервис автоматически читает параметры J/S/H/I через `awg showconf` внутри контейнера. Ключ `-i` у `docker exec` обязателен: preshared key передается процессу через stdin и не попадает в аргументы командной строки.

   Unit намеренно не включает `NoNewPrivileges=true`: этот флаг блокирует разрешенный вызов `sudo`. Объем повышения прав вместо этого ограничен точным списком команд в sudoers.

6. Поставьте Caddy, nginx или другой reverse proxy перед `127.0.0.1:8000`, включите HTTPS и только затем откройте сервис пользователям.

## ЮKassa

```dotenv
PAYMENT_PROVIDER=yookassa
YOOKASSA_SHOP_ID=...
YOOKASSA_SECRET_KEY=...
```

В кабинете ЮKassa добавьте HTTPS webhook:

```text
https://vpn.example.com/webhooks/yookassa
```

и событие `payment.succeeded`. Полученное уведомление не считается доказательством оплаты само по себе: сервис запрашивает объект платежа через API ЮKassa и только потом сверяет `status`, сумму, валюту и `metadata.internal_payment_id`. Создание платежа использует `Idempotence-Key`.

Перед реальным запуском нужно отдельно настроить онлайн-чеки/54-ФЗ в соответствии с вашим юрлицом и договором с платежным провайдером — данные чека зависят от налоговой схемы и намеренно не зашиты в MVP.

## Границы веб-управления

AmneziaWG построен на модели WireGuard peer. Сервер видит публичный ключ, endpoint, время последнего handshake и счетчики трафика; он может добавить или отозвать peer. Сервер не получает доступ к экрану клиента, файлам, процессам или удаленному выполнению команд. В админке «подключен» означает handshake не старше трех минут.

## Перед production

- заменить все секреты и хранить `.env` с правами `0600`;
- настроить резервные копии PostgreSQL и отдельно сохранить `ENCRYPTION_KEY`;
- добавить миграции Alembic перед первым изменением схемы после релиза;
- ограничить вход в админку по VPN/IP или добавить 2FA;
- настроить журналирование, мониторинг, уведомления об ошибках provisioning и rate limit на reverse proxy;
- проверить требования законодательства вашей юрисдикции к VPN, платежам, персональным данным и чекам;
- провести нагрузочный и внешний security-аудит.

## Структура

```text
app/
  main.py                    запуск, middleware, фоновые сверки
  models.py                  пользователи, платежи, подписки, peer-ключи
  services/payments.py       mock и ЮKassa
  services/provisioning.py   mock и native AmneziaWG
  services/lifecycle.py      бизнес-правила и истечение доступа
  web.py                     web/API endpoints
  templates/                 кабинет клиента и администратора
deploy/                      systemd и sudoers-примеры
tests/                       unit и end-to-end сценарии
```
