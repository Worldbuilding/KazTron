from .userstats import UserStats


def setup(bot):
    bot.add_cog(UserStats(bot))
