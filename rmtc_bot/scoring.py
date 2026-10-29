"""v1.1 arithmetic for given contributions; not a world or opponent simulator."""


def after_tick(previous, attack_damage=0, kill_awards=0, deaths=0, death_penalty=60):
    # Clamp once, after ALL batches and deaths in the tick. Never clamp at 500.
    return max(0.0, previous + attack_damage + kill_awards - death_penalty * deaths)


def combat_points(damage, cap=500):
    return 15.0 * min(max(0.0, damage) / cap, 1.0)


def marginal_value(previous, gain=0, deaths=0, cap=500, death_penalty=60, tie_weight=0):
    final = after_tick(previous, gain, deaths=deaths, death_penalty=death_penalty)
    # Extra damage has only tiebreak utility after the points cap; losing damage
    # above the cap still costs that utility. This is a policy weight, not points.
    extra = max(0.0, final - cap) - max(0.0, previous - cap)
    return combat_points(final, cap) - combat_points(previous, cap) + extra * tie_weight
