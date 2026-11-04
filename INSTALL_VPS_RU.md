# Отдельный VPS · Ubuntu 24.04 + Docker + бесплатный адрес

Если остаёшься на Bothost, используй `INSTALL_BOTHOST_RU.md`: эти команды там не нужны. Ниже — для собственного VPS с SSH/root или sudo. Для небольшого проекта разумный старт — 2 ГБ RAM и 20 ГБ SSD; фактическая нагрузка зависит от числа каналов и картинок. Сама модель ИИ работает во внешнем сервисе, локальная видеокарта не требуется.

## 1. Подключись и установи Docker

На своём компьютере открой терминал:

```sh
ssh root@IP_ТВОЕГО_СЕРВЕРА
```

Если Docker и Compose уже есть, проверь `docker compose version` и переходи дальше. На чистой Ubuntu 24.04 команды официального репозитория Docker:

```sh
sudo apt update
sudo apt install -y ca-certificates curl unzip
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
```

Создай файл `/etc/apt/sources.list.d/docker.sources`:

```sh
sudo tee /etc/apt/sources.list.d/docker.sources <<'EOF'
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: noble
Components: stable
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo docker compose version
```

`noble` здесь именно для Ubuntu 24.04. Для другой ОС используй её официальную инструкцию. Если Docker установлен из другого репозитория, не удаляй его и работающие контейнеры вслепую; следуй инструкции миграции поставщика.

Источник: https://docs.docker.com/engine/install/ubuntu/

## 2. Загрузи новый код

На КОМПЬЮТЕРЕ, из папки скачанного архива:

```sh
scp SMM_Web.zip root@IP_ТВОЕГО_СЕРВЕРА:/tmp/SMM_Web.zip
```

На СЕРВЕРЕ:

```sh
sudo mkdir -p /opt/smm-studio
sudo unzip /tmp/SMM_Web.zip -d /opt/smm-studio
cd /opt/smm-studio
sudo cp .env.example .env
sudo chmod 600 .env
```

Для первого запуска папка `/opt/smm-studio` должна быть новой. Для обновления смотри пункт 8 — не перезаписывай `.env` и данные.

## 3. Бесплатный адрес DuckDNS

1. Открой https://www.duckdns.org/ и войди своим способом.
2. Выбери свободное имя, например `my-content-studio`. Это пример; доступность имени проверяет DuckDNS.
3. В поле IPv4 укажи публичный IPv4 своего VPS и обнови запись.
4. Получится адрес вида `ИМЯ.duckdns.org`. Это бесплатный поддомен.
5. Не добавляй IPv6-запись, если он не настроен и не доступен извне. Если IP VPS изменится, обнови DNS вручную или настрой клиент DuckDNS по их инструкции.

DuckDNS не предоставляет сам сервер. Нужен твой VPS с доступными входящими 80 и 443. Для платформы Bothost этот способ не требуется — там используй их адрес.

## 4. Заполни ENV

```sh
sudo nano .env
```

| Ключ | Что указать |
|---|---|
| BOT_TOKEN | Токен твоего существующего бота |
| OWNER_ID | Твой числовой Telegram ID; при переносе должен совпасть со старым |
| SMM_DOMAIN | ИМЯ.duckdns.org — без https:// и без пути |
| SMM_WEB_URL | https://ИМЯ.duckdns.org |
| SMM_WEB_COOKIE_SECURE | 1 |
| SMM_HOST_DATA_DIR | ./persistent-data для новой установки, либо путь к твоим прежним данным |
| MANAGER_ENABLED | 0, пока Telegram-аккаунт менеджера не подключён |

Если аккаунт менеджера уже подключён — сохрани его API-переменные и сессию. `.env` не загружай в GitHub.

## 5. Сохрани прежние данные

Новая установка создаст базу сама. Для переноса **останови прежний бот** и возьми приватную копию его постоянной папки данных целиком: `smm.sqlite`, `config.json`, `credentials.json`, `host.json`, `manager.session`, `media/` и остальные пользовательские данные, если они есть.

В этой конфигурации Docker монтирует `SMM_HOST_DATA_DIR` в `/app/data`. Поэтому расположение для стандартной настройки должно быть:

```text
/opt/smm-studio/persistent-data/smm/smm.sqlite
/opt/smm-studio/persistent-data/smm/config.json
/opt/smm-studio/persistent-data/smm/credentials.json
/opt/smm-studio/persistent-data/smm/host.json
```

Если старая папка уже `/srv/mybot/data/smm`, укажи `SMM_HOST_DATA_DIR=/srv/mybot/data`. Если `smm.sqlite` лежит в `/srv/mybot/data` без вложенного `smm/`, сначала скопируй прежние данные в нужную структуру в НОВОЙ папке; оригинал оставь для отката. Не запускай контейнер на пустой базе, ожидая появления старых настроек.

При переносе с Bothost сначала сохрани приватный бэкап, останови старую копию и скачай данные через средства хостинга. Не открывай токены и файлы входа по публичной ссылке.

## 6. Открой сеть и запусти

В firewall/панели VPS разреши входящие TCP 80 и 443. SSH-доступ тоже должен остаться разрешён. Приложение на 8000 доступно только внутри Docker-сети; наружу публикуется Caddy. Если 80/443 уже заняты другим сайтом, встроенный Caddy не запустится: добавь маршрут в существующий прокси вместо второго процесса на тех же портах.

```sh
cd /opt/smm-studio
sudo docker compose up -d --build
sudo docker compose ps
sudo docker compose logs --tail=80 studio caddy
```

Caddy автоматически получает и обновляет сертификат для указанного адреса. До запуска DNS должен указывать на VPS, а 80/443 — быть доступными. В процессе выпуска сертификата потребуется доступ VPS к интернету.

Открой `https://ИМЯ.duckdns.org`. Отправь своему боту `/web`, введи username/ID и код. Проверь выбранную модель, каналы и очередь, затем продолжи СММ. Чтобы видеть статус живости HTTP: `https://ИМЯ.duckdns.org/healthz` — он не раскрывает данные аккаунта.

Источник HTTPS: https://caddyserver.com/docs/automatic-https
Источник DNS: https://www.duckdns.org/faqs.jsp

## 7. Менеджер — подключить позже

Когда получишь API ID/HASH своего приложения Telegram, впиши их в `.env` и `MANAGER_ENABLED=1`. Вход выполняется в интерактивном терминале. Сначала останови работающую студию, чтобы аккаунт не использовался одновременно двумя процессами:

```sh
sudo docker compose stop studio
sudo docker compose run --rm --no-deps studio python main.py manager-login
sudo docker compose up -d studio
```

Контейнер команды использует тот же постоянный диск. Введи телефон, код и 2FA-пароль в терминале, не в чат и не в публичный журнал. Сервис менеджера проверит подключение после запуска. Кампании запускаются вручную кнопкой «Старт».

## 8. Следующее обновление без потери данных

```sh
cd /opt/smm-studio
sudo docker compose exec studio python backup_data.py
sudo docker compose stop studio
```

Резервная копия появится внутри постоянной папки `backups/`. Сохрани её приватно вне VPS. Распакуй новый код в отдельный каталог, сравни конфигурацию, перенеси только исходники или переключи каталог развёртывания. Сохрани текущие `.env`, `SMM_HOST_DATA_DIR` и постоянные данные. Затем:

```sh
sudo docker compose up -d --build studio
sudo docker compose logs --tail=60 studio
```

Не используй `docker compose down -v`: это удаляет именованные тома, в том числе хранилище сертификатов. Приложение хранит пользовательские данные в bind-папке, которую тоже нельзя удалять или подменять.

## 9. Проверка и восстановление

- После штатной остановки СММ может остаться на паузе. Открой панель и нажми «Продолжить» после проверки.
- Пустая панель вместо старых данных: проверь путь постоянного диска и вложенность `smm/`.
- Сессия не сохраняется: заходи по HTTPS, проверь совпадение SMM_WEB_URL с адресом.
- Ошибка сертификата: проверь DNS, порты и журнал caddy, не обходи предупреждение браузера.
- Для отката останови новый процесс и восстанови старый код вместе с копией данных в отдельную папку; затем укажи эту папку в ENV. Не запускай старый и новый бот одновременно с одним токеном.

Docker отсутствует в среде подготовки архива: конфигурация проверена по структуре и документации, но реальная сборка контейнера и выпуск сертификата выполняются на твоём VPS. Python-функции и HTTP API протестированы отдельно.
