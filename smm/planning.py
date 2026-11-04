"""Publication times chosen by the owner; Moscow time is always explicit."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from .store import TZ


def publication_time(text, now):
    current=datetime.fromtimestamp(now,TZ)
    value=text.strip().lower()
    relative=re.fullmatch(r'через\s+(\d+)\s*(мин(?:ут[ауы]?)?|м|час(?:а|ов)?|ч)',value)
    try:
        if relative:
            amount=int(relative[1])*(3600 if relative[2].startswith('ч') else 60)
            result=current+timedelta(seconds=amount)
        elif re.fullmatch(r'\d{1,2}:\d{2}',value):
            hour,minute=map(int,value.split(':'))
            result=current.replace(hour=hour,minute=minute,second=0,microsecond=0)
        elif re.fullmatch(r'(?:сегодня|завтра)\s+\d{1,2}:\d{2}',value):
            day,clock=value.split();hour,minute=map(int,clock.split(':'))
            result=(current+timedelta(days=day=='завтра')).replace(hour=hour,minute=minute,second=0,microsecond=0)
        else:
            result=None
            for fmt in ('%d.%m.%Y %H:%M','%d.%m %H:%M','%Y-%m-%d %H:%M'):
                try:
                    result=(datetime.strptime(value+' '+str(current.year),'%d.%m %H:%M %Y') if fmt=='%d.%m %H:%M' else datetime.strptime(value,fmt)).replace(tzinfo=TZ)
                    break
                except ValueError: pass
            if result is None: raise ValueError()
    except (ValueError,OverflowError):
        raise ValueError('Укажи время: 18:30, завтра 12:00 или 05.11.2026 18:30. Все часы — МСК.') from None
    delta=result.timestamp()-now
    if delta<10:
        raise ValueError('Это время уже прошло или наступит слишком скоро. Для следующего дня напиши «завтра 12:00».')
    if delta>30*86400:
        raise ValueError('Выбери дату в пределах следующих 30 дней.')
    return int(result.timestamp())
