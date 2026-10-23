Дашборд

Страница с карточками (матчи, таблицы и т. д.). Данные собирает collect.py по расписанию, страница index.html просто показывает data.json.

Как это устроено
config.json — список блоков (что показывать и откуда). Главный файл для правок.
collect.py — сборщик: для каждого блока вызывает обработчик по полю type.
index.html — страница.
.github/workflows/update.yml — расписание: раз в час и в 11:40 по Москве, плюс при любом изменении файлов.
Добавить блок

В config.json в список cards добавь объект:

json
{ "id": "barca", "title": "Барселона — ближайший матч", "type": "team_next", "team": "Barcelona", "country": "Spain" }

Типы блоков сейчас:

type	что делает	параметры
team_next	ближайший матч команды (TheSportsDB)	team (англ. название), country
fd_table	таблица турнира (football-data.org)	comp (CL, PD, PL, BL1, SA, FL1…), cutoffs
fd_next	матчи турнира на N дней вперёд (football-data.org)	comp, days, first_day_only
slam_russians	ближайший шлем: невылетевшие игроки из топ-N, родившиеся в нужной стране, и их матч (ESPN)	slams, top, born_in
steam_upcoming	топ-N ожидаемых игр до конца года из «Популярных ожидаемых» Steam	count, scan
Убрать блок

Добавь в его объект "enabled": false (или удали объект целиком).

Если блок показывает ошибку

На странице под блоком будет написано, что не получилось, и показаны последние удачные данные. Подробности в GitHub: вкладка Actions → последний запуск → шаг «Собрать данные».

Ключи

Лежат в секретах репозитория (Settings → Secrets and variables → Actions): FOOTBALL_DATA_KEY — ключ football-data.org. В файлы ключи не записываются.
