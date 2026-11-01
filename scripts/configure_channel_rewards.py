"""Enable the agreed LEDOKOL onboarding rules after migrating to 0112.

Run inside the bot container: python -m scripts.configure_channel_rewards
Does not grant days or send notifications. Safe to run repeatedly.
"""

import asyncio

from sqlalchemy import select

from app.database.crud.system_setting import upsert_system_setting
from app.database.database import AsyncSessionLocal
from app.database.models import RequiredChannel, RewardTask


async def configure():
    async with AsyncSessionLocal() as db:
        channel = await db.scalar(
            select(RequiredChannel)
            .where(
                RequiredChannel.channel_link == 'https://t.me/LEDOKOL_CHANNEL',
                RequiredChannel.is_active.is_(True),
            )
            .with_for_update()
        )
        if not channel:
            raise RuntimeError('Active LEDOKOL_CHANNEL must be configured before enabling trial membership')
        channel.disable_trial_on_leave = True
        channel.disable_paid_on_leave = False
        task = await db.scalar(
            select(RewardTask)
            .where(
                RewardTask.kind == 'channel_subscription',
                RewardTask.channel_id == channel.channel_id,
            )
            .order_by(RewardTask.id)
        )
        if task is None:
            db.add(
                RewardTask(
                    kind='channel_subscription',
                    title='Подпишись на канал LEDOKOL — получи +1 день',
                    description='Подпишитесь на @LEDOKOL_CHANNEL и нажмите «Проверить подписку». '
                    'Один дополнительный день к подписке можно получить один раз, '
                    'в том числе если вы уже подписаны на канал.',
                    reward_days=1,
                    channel_id=channel.channel_id,
                    action_url=channel.channel_link,
                    is_active=True,
                    sort_order=-10,
                )
            )
        for key, value in {
            'CHANNEL_IS_REQUIRED_SUB': 'false',
            'CHANNEL_TRIAL_REQUIRED': 'true',
            'CHANNEL_REQUIRED_FOR_ALL': 'false',
            'CHANNEL_DISABLE_TRIAL_ON_UNSUBSCRIBE': 'true',
            'FIRST_PURCHASE_OFFER_ENABLED': 'true',
            'REWARD_TASKS_FOR_ALL_USERS': 'true',
        }.items():
            await upsert_system_setting(db, key, value)
        await db.commit()
        print('Channel reward task configured; restart the bot to load onboarding settings.')


if __name__ == '__main__':
    asyncio.run(configure())
