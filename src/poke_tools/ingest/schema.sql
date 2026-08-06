-- pokedex.sqlite schema (PLAN.md §1.2).
--
-- This file is the single source of truth for both the DDL *and* the column
-- order the CSV loader expects. `pokeapi_csv.py` reads `PRAGMA table_info` off
-- each table it is about to fill and asserts the downloaded CSV header matches
-- exactly, so a column added or reordered upstream fails the build loudly
-- instead of silently shifting every value one place to the left. Consequence:
-- **columns must be declared in the same order the upstream CSV emits them.**
--
-- Typed columns and real foreign keys are deliberate — PLAN.md §0.3's whole
-- argument for the veekun CSVs over the PokéAPI JSON dump is that they give a
-- clean relational model for free, and storing everything as TEXT would throw
-- away the only thing the CSVs buy us.
--
-- Tables are declared in load order (parents before children) because the
-- loader runs with `PRAGMA foreign_keys = ON`.


-- ---------------------------------------------------------------------------
-- Languages, regions, generations, versions
-- ---------------------------------------------------------------------------

-- Loaded in full. Every other localized table is filtered to English at ingest
-- (see ENGLISH_LANGUAGE_ID in pokeapi_csv.py); this table stays complete so the
-- `local_language_id` foreign keys remain meaningful and re-widening later is a
-- one-constant change.
CREATE TABLE languages (
    id          INTEGER PRIMARY KEY,
    iso639      TEXT NOT NULL,
    iso3166     TEXT NOT NULL,
    identifier  TEXT NOT NULL,
    official    INTEGER NOT NULL,
    "order"     INTEGER
);

CREATE TABLE regions (
    id          INTEGER PRIMARY KEY,
    identifier  TEXT NOT NULL
);

CREATE TABLE region_names (
    region_id           INTEGER NOT NULL REFERENCES regions (id),
    local_language_id   INTEGER NOT NULL REFERENCES languages (id),
    name                TEXT NOT NULL,
    PRIMARY KEY (region_id, local_language_id)
);

CREATE TABLE generations (
    id              INTEGER PRIMARY KEY,
    main_region_id  INTEGER NOT NULL REFERENCES regions (id),
    identifier      TEXT NOT NULL
);

CREATE TABLE version_groups (
    id              INTEGER PRIMARY KEY,
    identifier      TEXT NOT NULL,
    generation_id   INTEGER NOT NULL REFERENCES generations (id),
    "order"         INTEGER
);

-- PLAN.md §0.6.3: encounters key on `version_id`, not `version_group_id`, so
-- this table is the bridge `where_to_find` has to cross. HGSS is one version
-- group (10) spanning two versions (15 HeartGold, 16 SoulSilver).
CREATE TABLE versions (
    id                  INTEGER PRIMARY KEY,
    version_group_id    INTEGER NOT NULL REFERENCES version_groups (id),
    identifier          TEXT NOT NULL
);

CREATE TABLE version_names (
    version_id          INTEGER NOT NULL REFERENCES versions (id),
    local_language_id   INTEGER NOT NULL REFERENCES languages (id),
    name                TEXT NOT NULL,
    PRIMARY KEY (version_id, local_language_id)
);

CREATE TABLE version_group_regions (
    version_group_id    INTEGER NOT NULL REFERENCES version_groups (id),
    region_id           INTEGER NOT NULL REFERENCES regions (id),
    PRIMARY KEY (version_group_id, region_id)
);


-- ---------------------------------------------------------------------------
-- Locations
--
-- Declared before the species tables purely for load order: `pokemon_evolution`
-- carries a `location_id`, and putting `locations` first lets that stay a real
-- foreign key.
-- ---------------------------------------------------------------------------

CREATE TABLE locations (
    id          INTEGER PRIMARY KEY,
    region_id   INTEGER REFERENCES regions (id),
    identifier  TEXT NOT NULL
);

CREATE TABLE location_names (
    location_id         INTEGER NOT NULL REFERENCES locations (id),
    local_language_id   INTEGER NOT NULL REFERENCES languages (id),
    name                TEXT NOT NULL,
    subtitle            TEXT,
    PRIMARY KEY (location_id, local_language_id)
);

CREATE TABLE location_areas (
    id          INTEGER PRIMARY KEY,
    location_id INTEGER NOT NULL REFERENCES locations (id),
    game_index  INTEGER NOT NULL,
    identifier  TEXT
);

-- No primary key, deliberately. Upstream violates the (location_area_id,
-- local_language_id) key it implies: six areas carry two English rows each --
-- the five Kalos Terminus Cave areas (e.g. location_area 754 is both "Terminus
-- Cave (unknown area 343)" and "Terminus Cave (1F)") plus Unova's roaming-unova
-- area 1169 ("Rumination Field" and "Roaming Unova"). Keeping both preserves
-- upstream fidelity and leaves the choice to the query layer; declaring a key
-- the data does not satisfy would only mean the build refuses to run.
CREATE TABLE location_area_prose (
    location_area_id    INTEGER NOT NULL REFERENCES location_areas (id),
    local_language_id   INTEGER NOT NULL REFERENCES languages (id),
    name                TEXT
);

CREATE INDEX idx_location_area_prose_area
    ON location_area_prose (location_area_id, local_language_id);


-- ---------------------------------------------------------------------------
-- Types and the (gen-correct) efficacy chart
-- ---------------------------------------------------------------------------

CREATE TABLE move_damage_classes (
    id          INTEGER PRIMARY KEY,
    identifier  TEXT NOT NULL
);

CREATE TABLE types (
    id                  INTEGER PRIMARY KEY,
    identifier          TEXT NOT NULL,
    generation_id       INTEGER NOT NULL REFERENCES generations (id),
    damage_class_id     INTEGER REFERENCES move_damage_classes (id)
);

CREATE TABLE type_names (
    type_id             INTEGER NOT NULL REFERENCES types (id),
    local_language_id   INTEGER NOT NULL REFERENCES languages (id),
    name                TEXT NOT NULL,
    PRIMARY KEY (type_id, local_language_id)
);

CREATE TABLE type_efficacy (
    damage_type_id  INTEGER NOT NULL REFERENCES types (id),
    target_type_id  INTEGER NOT NULL REFERENCES types (id),
    damage_factor   INTEGER NOT NULL,
    PRIMARY KEY (damage_type_id, target_type_id)
);

-- PLAN.md §0.6.2: six rows total (Gen 1 and Gen 5 changes only). A tiny file
-- here is correct, not a truncated download.
CREATE TABLE type_efficacy_past (
    damage_type_id  INTEGER NOT NULL REFERENCES types (id),
    target_type_id  INTEGER NOT NULL REFERENCES types (id),
    damage_factor   INTEGER NOT NULL,
    generation_id   INTEGER NOT NULL REFERENCES generations (id),
    PRIMARY KEY (damage_type_id, target_type_id, generation_id)
);


-- ---------------------------------------------------------------------------
-- Stats, natures, growth rates
-- ---------------------------------------------------------------------------

CREATE TABLE stats (
    id              INTEGER PRIMARY KEY,
    damage_class_id INTEGER REFERENCES move_damage_classes (id),
    identifier      TEXT NOT NULL,
    is_battle_only  INTEGER NOT NULL,
    game_index      INTEGER
);

CREATE TABLE stat_names (
    stat_id             INTEGER NOT NULL REFERENCES stats (id),
    local_language_id   INTEGER NOT NULL REFERENCES languages (id),
    name                TEXT NOT NULL,
    PRIMARY KEY (stat_id, local_language_id)
);

CREATE TABLE contest_types (
    id          INTEGER PRIMARY KEY,
    identifier  TEXT NOT NULL
);

CREATE TABLE contest_effects (
    id      INTEGER PRIMARY KEY,
    appeal  INTEGER NOT NULL,
    jam     INTEGER NOT NULL
);

CREATE TABLE super_contest_effects (
    id      INTEGER PRIMARY KEY,
    appeal  INTEGER NOT NULL
);

CREATE TABLE natures (
    id                  INTEGER PRIMARY KEY,
    identifier          TEXT NOT NULL,
    decreased_stat_id   INTEGER NOT NULL REFERENCES stats (id),
    increased_stat_id   INTEGER NOT NULL REFERENCES stats (id),
    hates_flavor_id     INTEGER NOT NULL REFERENCES contest_types (id),
    likes_flavor_id     INTEGER NOT NULL REFERENCES contest_types (id),
    game_index          INTEGER NOT NULL
);

CREATE TABLE nature_names (
    nature_id           INTEGER NOT NULL REFERENCES natures (id),
    local_language_id   INTEGER NOT NULL REFERENCES languages (id),
    name                TEXT NOT NULL,
    PRIMARY KEY (nature_id, local_language_id)
);

CREATE TABLE growth_rates (
    id          INTEGER PRIMARY KEY,
    identifier  TEXT NOT NULL,
    formula     TEXT NOT NULL
);

CREATE TABLE growth_rate_prose (
    growth_rate_id      INTEGER NOT NULL REFERENCES growth_rates (id),
    local_language_id   INTEGER NOT NULL REFERENCES languages (id),
    name                TEXT NOT NULL,
    PRIMARY KEY (growth_rate_id, local_language_id)
);


-- ---------------------------------------------------------------------------
-- Moves
-- ---------------------------------------------------------------------------

CREATE TABLE move_targets (
    id          INTEGER PRIMARY KEY,
    identifier  TEXT NOT NULL
);

CREATE TABLE move_effects (
    id  INTEGER PRIMARY KEY
);

CREATE TABLE move_effect_prose (
    move_effect_id      INTEGER NOT NULL REFERENCES move_effects (id),
    local_language_id   INTEGER NOT NULL REFERENCES languages (id),
    short_effect        TEXT,
    effect              TEXT,
    PRIMARY KEY (move_effect_id, local_language_id)
);

CREATE TABLE move_meta_categories (
    id          INTEGER PRIMARY KEY,
    identifier  TEXT NOT NULL
);

CREATE TABLE move_meta_ailments (
    id          INTEGER PRIMARY KEY,
    identifier  TEXT NOT NULL
);

-- Current (latest-generation) values. Gen-correct values for Emerald / HGSS are
-- reconstructed by walking `move_changelog` *backwards* from here — PLAN.md
-- §0.6.2 flags getting that direction wrong as a plausible-but-silent bug and
-- asks for its own eval. Phase 1b owns that query.
CREATE TABLE moves (
    id                      INTEGER PRIMARY KEY,
    identifier              TEXT NOT NULL,
    generation_id           INTEGER NOT NULL REFERENCES generations (id),
    type_id                 INTEGER NOT NULL REFERENCES types (id),
    power                   INTEGER,
    pp                      INTEGER,
    accuracy                INTEGER,
    priority                INTEGER NOT NULL,
    target_id               INTEGER NOT NULL REFERENCES move_targets (id),
    damage_class_id         INTEGER NOT NULL REFERENCES move_damage_classes (id),
    -- Nullable: 93 upstream moves (Gen 9 additions and Shadow moves) carry an
    -- empty effect_id. get_move must tolerate a move with no effect row rather
    -- than assume the join always lands.
    effect_id               INTEGER REFERENCES move_effects (id),
    effect_chance           INTEGER,
    contest_type_id         INTEGER REFERENCES contest_types (id),
    contest_effect_id       INTEGER REFERENCES contest_effects (id),
    super_contest_effect_id INTEGER REFERENCES super_contest_effects (id)
);

CREATE TABLE move_names (
    move_id             INTEGER NOT NULL REFERENCES moves (id),
    local_language_id   INTEGER NOT NULL REFERENCES languages (id),
    name                TEXT NOT NULL,
    PRIMARY KEY (move_id, local_language_id)
);

CREATE TABLE move_meta (
    move_id             INTEGER PRIMARY KEY REFERENCES moves (id),
    meta_category_id    INTEGER NOT NULL REFERENCES move_meta_categories (id),
    meta_ailment_id     INTEGER NOT NULL REFERENCES move_meta_ailments (id),
    min_hits            INTEGER,
    max_hits            INTEGER,
    min_turns           INTEGER,
    max_turns           INTEGER,
    drain               INTEGER NOT NULL,
    healing             INTEGER NOT NULL,
    crit_rate           INTEGER NOT NULL,
    ailment_chance      INTEGER NOT NULL,
    flinch_chance       INTEGER NOT NULL,
    stat_chance         INTEGER NOT NULL
);

-- PLAN.md §0.6.2. NOT `move_past_values` — that is a PokéAPI REST field name
-- with no CSV counterpart. Each row records what the move looked like *before*
-- the change introduced in `changed_in_version_group_id`; NULL = unchanged.
CREATE TABLE move_changelog (
    move_id                     INTEGER NOT NULL REFERENCES moves (id),
    changed_in_version_group_id INTEGER NOT NULL REFERENCES version_groups (id),
    type_id                     INTEGER REFERENCES types (id),
    power                       INTEGER,
    pp                          INTEGER,
    accuracy                    INTEGER,
    priority                    INTEGER,
    target_id                   INTEGER REFERENCES move_targets (id),
    effect_id                   INTEGER REFERENCES move_effects (id),
    effect_chance               INTEGER,
    PRIMARY KEY (move_id, changed_in_version_group_id)
);


-- ---------------------------------------------------------------------------
-- Items and machines
-- ---------------------------------------------------------------------------

CREATE TABLE item_pockets (
    id          INTEGER PRIMARY KEY,
    identifier  TEXT NOT NULL
);

CREATE TABLE item_categories (
    id          INTEGER PRIMARY KEY,
    pocket_id   INTEGER NOT NULL REFERENCES item_pockets (id),
    identifier  TEXT NOT NULL
);

CREATE TABLE item_category_prose (
    item_category_id    INTEGER NOT NULL REFERENCES item_categories (id),
    local_language_id   INTEGER NOT NULL REFERENCES languages (id),
    name                TEXT NOT NULL,
    PRIMARY KEY (item_category_id, local_language_id)
);

CREATE TABLE item_fling_effects (
    id          INTEGER PRIMARY KEY,
    identifier  TEXT NOT NULL
);

CREATE TABLE items (
    id              INTEGER PRIMARY KEY,
    identifier      TEXT NOT NULL,
    category_id     INTEGER NOT NULL REFERENCES item_categories (id),
    cost            INTEGER NOT NULL,
    fling_power     INTEGER,
    fling_effect_id INTEGER REFERENCES item_fling_effects (id)
);

CREATE TABLE item_names (
    item_id             INTEGER NOT NULL REFERENCES items (id),
    local_language_id   INTEGER NOT NULL REFERENCES languages (id),
    name                TEXT NOT NULL,
    PRIMARY KEY (item_id, local_language_id)
);

-- `get_item` answers "effect, fling, category" from here (PLAN.md §1.3).
-- Acquisition — "where do I find Leftovers" — has no field anywhere in the CSV
-- set (PLAN.md §0.2) and is precisely why Tier 2 exists.
CREATE TABLE item_prose (
    item_id             INTEGER NOT NULL REFERENCES items (id),
    local_language_id   INTEGER NOT NULL REFERENCES languages (id),
    short_effect        TEXT,
    effect              TEXT,
    PRIMARY KEY (item_id, local_language_id)
);

CREATE TABLE item_flags (
    id          INTEGER PRIMARY KEY,
    identifier  TEXT NOT NULL
);

CREATE TABLE item_flag_map (
    item_id         INTEGER NOT NULL REFERENCES items (id),
    item_flag_id    INTEGER NOT NULL REFERENCES item_flags (id),
    PRIMARY KEY (item_id, item_flag_id)
);

-- Which generations an item actually exists in. `get_item` reads nothing else
-- from this table -- the `game_index` itself is an internal ROM index no tool
-- surfaces -- but its *presence* is the only structured signal in the whole CSV
-- set that answers "is this item in this game at all".
--
-- Without it `get_item('life-orb', 'emerald')` returns a clean card stamped
-- `game: emerald` for a Generation 4 item, which is an affirmative assertion of
-- something false rather than a mere omission, and feeds straight into the
-- Phase 4 team_check item-availability logic.
--
-- **A missing generation row and a missing item are different facts.** 49 of
-- 2,222 items (Oak's Parcel, Metal Alloy, every Mega Stone that never shipped a
-- game index, ...) have *no* rows here at all. Treating "no gen-3 row" and "no
-- rows whatsoever" alike would turn those 49 into false does-not-exist claims --
-- the same PLAN.md §3.9 error pointing the other way -- so `get_item` reports
-- them as unknown, not absent.
--
-- No primary key, deliberately, matching `location_area_prose` above: upstream
-- violates the (item_id, generation_id) key this table implies. Five pairs
-- repeat -- elixir and kings-rock each carry two Gen 2 indexes (21/65 and
-- 82/221), deep-sea-tooth two Gen 5 indexes (226/227), and aloraichium-z--held
-- and holo-caster--red are exact duplicate rows. Declaring the key would only
-- mean the build refuses to run; nothing here reads `game_index`, so the
-- duplicates are harmless to the existence test.
CREATE TABLE item_game_indices (
    item_id         INTEGER NOT NULL REFERENCES items (id),
    generation_id   INTEGER NOT NULL REFERENCES generations (id),
    game_index      INTEGER NOT NULL
);

-- PLAN.md §0.6.4: `machine_number` is stored raw on purpose. The TM/HM label is
-- derived at query time (`n <= 100 -> TM{n:02d}`, else `HM{n-100:02d}`); baking
-- the string in would be a lossy transform of the only column that carries the
-- ordering.
CREATE TABLE machines (
    machine_number      INTEGER NOT NULL,
    version_group_id    INTEGER NOT NULL REFERENCES version_groups (id),
    item_id             INTEGER NOT NULL REFERENCES items (id),
    move_id             INTEGER NOT NULL REFERENCES moves (id),
    PRIMARY KEY (machine_number, version_group_id)
);


-- ---------------------------------------------------------------------------
-- Abilities and egg groups
-- ---------------------------------------------------------------------------

CREATE TABLE abilities (
    id              INTEGER PRIMARY KEY,
    identifier      TEXT NOT NULL,
    generation_id   INTEGER NOT NULL REFERENCES generations (id),
    is_main_series  INTEGER NOT NULL
);

CREATE TABLE ability_names (
    ability_id          INTEGER NOT NULL REFERENCES abilities (id),
    local_language_id   INTEGER NOT NULL REFERENCES languages (id),
    name                TEXT NOT NULL,
    PRIMARY KEY (ability_id, local_language_id)
);

CREATE TABLE ability_prose (
    ability_id          INTEGER NOT NULL REFERENCES abilities (id),
    local_language_id   INTEGER NOT NULL REFERENCES languages (id),
    short_effect        TEXT,
    effect              TEXT,
    PRIMARY KEY (ability_id, local_language_id)
);

CREATE TABLE egg_groups (
    id          INTEGER PRIMARY KEY,
    identifier  TEXT NOT NULL
);

CREATE TABLE egg_group_prose (
    egg_group_id        INTEGER NOT NULL REFERENCES egg_groups (id),
    local_language_id   INTEGER NOT NULL REFERENCES languages (id),
    name                TEXT NOT NULL,
    PRIMARY KEY (egg_group_id, local_language_id)
);


-- ---------------------------------------------------------------------------
-- Species and Pokémon
-- ---------------------------------------------------------------------------

CREATE TABLE pokemon_colors (
    id          INTEGER PRIMARY KEY,
    identifier  TEXT NOT NULL
);

CREATE TABLE pokemon_shapes (
    id          INTEGER PRIMARY KEY,
    identifier  TEXT NOT NULL
);

CREATE TABLE pokemon_habitats (
    id          INTEGER PRIMARY KEY,
    identifier  TEXT NOT NULL
);

CREATE TABLE evolution_chains (
    id                      INTEGER PRIMARY KEY,
    baby_trigger_item_id    INTEGER REFERENCES items (id)
);

-- `evolves_from_species_id` is the base-form walk PLAN.md §0.2 makes mandatory
-- for egg moves (Garchomp has none; Gible has four). The self foreign key is
-- DEFERRABLE because the CSV is ordered by id while baby forms carry *higher*
-- ids than the Pokémon that evolve from them — Pikachu (25) points forward at
-- Pichu (172), so an immediate check would reject the load mid-file.
CREATE TABLE pokemon_species (
    id                      INTEGER PRIMARY KEY,
    identifier              TEXT NOT NULL,
    generation_id           INTEGER REFERENCES generations (id),
    evolves_from_species_id INTEGER REFERENCES pokemon_species (id)
                                DEFERRABLE INITIALLY DEFERRED,
    evolution_chain_id      INTEGER REFERENCES evolution_chains (id),
    color_id                INTEGER REFERENCES pokemon_colors (id),
    shape_id                INTEGER REFERENCES pokemon_shapes (id),
    habitat_id              INTEGER REFERENCES pokemon_habitats (id),
    gender_rate             INTEGER,
    capture_rate            INTEGER,
    base_happiness          INTEGER,
    is_baby                 INTEGER,
    hatch_counter           INTEGER,
    has_gender_differences  INTEGER,
    growth_rate_id          INTEGER REFERENCES growth_rates (id),
    forms_switchable        INTEGER,
    is_legendary            INTEGER,
    is_mythical             INTEGER,
    "order"                 INTEGER,
    conquest_order          INTEGER
);

CREATE TABLE pokemon_species_names (
    pokemon_species_id  INTEGER NOT NULL REFERENCES pokemon_species (id),
    local_language_id   INTEGER NOT NULL REFERENCES languages (id),
    name                TEXT NOT NULL,
    genus               TEXT,
    PRIMARY KEY (pokemon_species_id, local_language_id)
);

CREATE TABLE pokemon (
    id              INTEGER PRIMARY KEY,
    identifier      TEXT NOT NULL,
    species_id      INTEGER REFERENCES pokemon_species (id),
    height          INTEGER,
    weight          INTEGER,
    base_experience INTEGER,
    "order"         INTEGER,
    is_default      INTEGER
);

CREATE TABLE pokemon_forms (
    id                              INTEGER PRIMARY KEY,
    identifier                      TEXT NOT NULL,
    form_identifier                 TEXT,
    pokemon_id                      INTEGER NOT NULL REFERENCES pokemon (id),
    introduced_in_version_group_id  INTEGER REFERENCES version_groups (id),
    is_default                      INTEGER NOT NULL,
    is_battle_only                  INTEGER NOT NULL,
    is_mega                         INTEGER NOT NULL,
    form_order                      INTEGER NOT NULL,
    "order"                         INTEGER NOT NULL
);

CREATE TABLE pokemon_stats (
    pokemon_id  INTEGER NOT NULL REFERENCES pokemon (id),
    stat_id     INTEGER NOT NULL REFERENCES stats (id),
    base_stat   INTEGER NOT NULL,
    effort      INTEGER NOT NULL,
    PRIMARY KEY (pokemon_id, stat_id)
);

CREATE TABLE pokemon_types (
    pokemon_id  INTEGER NOT NULL REFERENCES pokemon (id),
    type_id     INTEGER NOT NULL REFERENCES types (id),
    slot        INTEGER NOT NULL,
    PRIMARY KEY (pokemon_id, slot)
);

CREATE TABLE pokemon_abilities (
    pokemon_id  INTEGER NOT NULL REFERENCES pokemon (id),
    ability_id  INTEGER NOT NULL REFERENCES abilities (id),
    is_hidden   INTEGER NOT NULL,
    slot        INTEGER NOT NULL,
    PRIMARY KEY (pokemon_id, slot)
);

CREATE TABLE pokemon_egg_groups (
    species_id      INTEGER NOT NULL REFERENCES pokemon_species (id),
    egg_group_id    INTEGER NOT NULL REFERENCES egg_groups (id),
    PRIMARY KEY (species_id, egg_group_id)
);

-- Held-by-wild rarity: the one clause of PLAN.md §1.3's `get_item` line that had
-- no table behind it. `rarity` is a percentage (1..100) of wild members of that
-- species carrying the item -- Snorlax holds Leftovers at 100 in both launch
-- games, which is the canonical answer to "where do I get Leftovers in Gen 2/3/4"
-- that PokéAPI otherwise cannot give.
--
-- **Keys on `version_id`, not `version_group_id`** -- the same ID-space split as
-- `encounters` (PLAN.md §0.6.3), so HGSS must union 15 and 16 and Emerald is 9
-- alone. Verified: no HGSS holding is version-asymmetric today, but the union is
-- written generically rather than assuming that.
--
-- `pokemon_id` is form-level, so a species with battle-only forms repeats: wild
-- Castform holds Mystic Water as four rows (castform plus rainy/snowy/sunny) at
-- an identical rarity. `get_item` collapses those to the species, keyed on
-- rarity so a genuine per-form disagreement would split into two rows rather
-- than silently pick one.
CREATE TABLE pokemon_items (
    pokemon_id      INTEGER NOT NULL REFERENCES pokemon (id),
    version_id      INTEGER NOT NULL REFERENCES versions (id),
    item_id         INTEGER NOT NULL REFERENCES items (id),
    rarity          INTEGER NOT NULL,
    PRIMARY KEY (pokemon_id, version_id, item_id)
);


-- ---------------------------------------------------------------------------
-- Gen-correct species data: the three `*_past` tables
-- ---------------------------------------------------------------------------
--
-- `pokemon_types`, `pokemon_stats` and `pokemon_abilities` hold *current*
-- (latest-generation) values only, exactly like `moves` does. Without these
-- three overlays `find_pokemon` answers every HGSS and Emerald question with
-- Gen 9 data — Clefable comes back Fairy in HeartGold (it was Normal until
-- Gen 6), every species advertises a Gen 5 hidden ability in Emerald, and the
-- Gen 6 base-stat revisions are reported for Gen 3/4 games. Each of those is a
-- plausible-looking wrong answer that propagates straight into type coverage
-- and team advice, which is the failure mode PLAN.md §0 exists to prevent.
--
-- **Resolution direction — note it is the opposite of `move_changelog`.** A row
-- here records the values that held **up to and including** `generation_id`,
-- so the value for a target generation G is the row with the *smallest*
-- `generation_id >= G`, falling back to the current table when there is none.
-- `move_changelog` instead keys on the version group the change was introduced
-- *in*. Mixing the two conventions produces off-by-one-generation answers with
-- no error, so the two are resolved by separate, separately-tested helpers in
-- `db.py`.
--
-- Verified against the live CSVs: 36 / 235 / 569 rows, key-unique on the
-- primary keys declared below.

CREATE TABLE pokemon_types_past (
    pokemon_id      INTEGER NOT NULL REFERENCES pokemon (id),
    generation_id   INTEGER NOT NULL REFERENCES generations (id),
    type_id         INTEGER NOT NULL REFERENCES types (id),
    slot            INTEGER NOT NULL,
    PRIMARY KEY (pokemon_id, generation_id, slot)
);

CREATE TABLE pokemon_stats_past (
    pokemon_id      INTEGER NOT NULL REFERENCES pokemon (id),
    generation_id   INTEGER NOT NULL REFERENCES generations (id),
    stat_id         INTEGER NOT NULL REFERENCES stats (id),
    base_stat       INTEGER NOT NULL,
    effort          INTEGER NOT NULL,
    PRIMARY KEY (pokemon_id, generation_id, stat_id)
);

-- `ability_id` is nullable and empty in 540 of 569 upstream rows: the row exists
-- to say "this slot held *no* ability in that generation", which is how the
-- absence of hidden abilities before Gen 5 is encoded. A NOT NULL here would
-- reject the load; treating a missing row as "unchanged" would report Gen 9
-- hidden abilities for Emerald.
CREATE TABLE pokemon_abilities_past (
    pokemon_id      INTEGER NOT NULL REFERENCES pokemon (id),
    generation_id   INTEGER NOT NULL REFERENCES generations (id),
    ability_id      INTEGER REFERENCES abilities (id),
    is_hidden       INTEGER NOT NULL,
    slot            INTEGER NOT NULL,
    PRIMARY KEY (pokemon_id, generation_id, slot)
);


-- ---------------------------------------------------------------------------
-- Learnsets — the centrepiece
-- ---------------------------------------------------------------------------

CREATE TABLE pokemon_move_methods (
    id          INTEGER PRIMARY KEY,
    identifier  TEXT NOT NULL
);

CREATE TABLE pokemon_move_method_prose (
    pokemon_move_method_id  INTEGER NOT NULL REFERENCES pokemon_move_methods (id),
    local_language_id       INTEGER NOT NULL REFERENCES languages (id),
    name                    TEXT NOT NULL,
    description             TEXT,
    PRIMARY KEY (pokemon_move_method_id, local_language_id)
);

-- PLAN.md §0.6.5: the methods that matter are 1 level-up, 2 egg, 3 tutor,
-- 4 machine.
--
-- The primary key doubles as the duplicate guard for a re-run. The five-column
-- combination is unique across all 638,321 upstream rows and `level` is never
-- empty (both verified against the live CSV before this schema was written), so
-- it cannot degrade into SQLite's NULL-in-primary-key loophole.
CREATE TABLE pokemon_moves (
    pokemon_id              INTEGER NOT NULL REFERENCES pokemon (id),
    version_group_id        INTEGER NOT NULL REFERENCES version_groups (id),
    move_id                 INTEGER NOT NULL REFERENCES moves (id),
    pokemon_move_method_id  INTEGER NOT NULL REFERENCES pokemon_move_methods (id),
    level                   INTEGER NOT NULL,
    "order"                 INTEGER,
    mastery                 INTEGER,
    PRIMARY KEY (pokemon_id, version_group_id, move_id, pokemon_move_method_id, level)
);


-- ---------------------------------------------------------------------------
-- Evolution
-- ---------------------------------------------------------------------------

CREATE TABLE genders (
    id          INTEGER PRIMARY KEY,
    identifier  TEXT NOT NULL
);

CREATE TABLE evolution_triggers (
    id          INTEGER PRIMARY KEY,
    identifier  TEXT NOT NULL
);

CREATE TABLE evolution_trigger_prose (
    evolution_trigger_id    INTEGER NOT NULL REFERENCES evolution_triggers (id),
    local_language_id       INTEGER NOT NULL REFERENCES languages (id),
    name                    TEXT NOT NULL,
    PRIMARY KEY (evolution_trigger_id, local_language_id)
);

-- `version_group_id` here is what makes `find_pokemon`'s "evolution with
-- per-game method" (PLAN.md §1.3) answerable: Gen 4 added trade-with-item and
-- location-based evolutions that do not exist in Emerald.
CREATE TABLE pokemon_evolution (
    id                      INTEGER PRIMARY KEY,
    evolved_species_id      INTEGER NOT NULL REFERENCES pokemon_species (id),
    evolution_trigger_id    INTEGER NOT NULL REFERENCES evolution_triggers (id),
    version_group_id        INTEGER REFERENCES version_groups (id),
    is_default              INTEGER,
    trigger_item_id         INTEGER REFERENCES items (id),
    minimum_level           INTEGER,
    gender_id               INTEGER REFERENCES genders (id),
    location_id             INTEGER REFERENCES locations (id),
    held_item_id            INTEGER REFERENCES items (id),
    time_of_day             TEXT,
    known_move_id           INTEGER REFERENCES moves (id),
    known_move_type_id      INTEGER REFERENCES types (id),
    minimum_happiness       INTEGER,
    minimum_beauty          INTEGER,
    minimum_affection       INTEGER,
    relative_physical_stats INTEGER,
    party_species_id        INTEGER REFERENCES pokemon_species (id),
    party_type_id           INTEGER REFERENCES types (id),
    trade_species_id        INTEGER REFERENCES pokemon_species (id),
    needs_overworld_rain    INTEGER,
    turn_upside_down        INTEGER,
    needs_multiplayer       INTEGER,
    near_special_rock       INTEGER,
    region_id               INTEGER REFERENCES regions (id),
    base_form_id            INTEGER REFERENCES pokemon_forms (id),
    evolved_form_id         INTEGER REFERENCES pokemon_forms (id),
    used_move_id            INTEGER REFERENCES moves (id),
    minimum_move_count      INTEGER,
    minimum_steps           INTEGER,
    minimum_damage_taken    INTEGER
);


-- ---------------------------------------------------------------------------
-- Encounters
-- ---------------------------------------------------------------------------

CREATE TABLE encounter_methods (
    id          INTEGER PRIMARY KEY,
    identifier  TEXT NOT NULL,
    "order"     INTEGER NOT NULL
);

CREATE TABLE encounter_method_prose (
    encounter_method_id INTEGER NOT NULL REFERENCES encounter_methods (id),
    local_language_id   INTEGER NOT NULL REFERENCES languages (id),
    name                TEXT NOT NULL,
    PRIMARY KEY (encounter_method_id, local_language_id)
);

CREATE TABLE encounter_conditions (
    id          INTEGER PRIMARY KEY,
    identifier  TEXT NOT NULL
);

CREATE TABLE encounter_condition_values (
    id                      INTEGER PRIMARY KEY,
    encounter_condition_id  INTEGER NOT NULL REFERENCES encounter_conditions (id),
    identifier              TEXT NOT NULL,
    is_default              INTEGER NOT NULL
);

-- Rarity lives here, not on `encounters` (PLAN.md §0.6.3). Note this table
-- keys on version_*group*_id while `encounters` keys on version_id.
CREATE TABLE encounter_slots (
    id                  INTEGER PRIMARY KEY,
    version_group_id    INTEGER NOT NULL REFERENCES version_groups (id),
    encounter_method_id INTEGER NOT NULL REFERENCES encounter_methods (id),
    slot                INTEGER,
    rarity              INTEGER
);

-- PLAN.md §0.6.3: **`version_id`, not `version_group_id`.** Every other table in
-- this schema keys on the version group; mixing the two ID spaces here yields a
-- silently wrong answer rather than a crash, which is why the indexes below are
-- all built on `version_id`. HGSS must union 15 and 16; Emerald is 9 alone.
CREATE TABLE encounters (
    id                  INTEGER PRIMARY KEY,
    version_id          INTEGER NOT NULL REFERENCES versions (id),
    location_area_id    INTEGER NOT NULL REFERENCES location_areas (id),
    encounter_slot_id   INTEGER NOT NULL REFERENCES encounter_slots (id),
    pokemon_id          INTEGER NOT NULL REFERENCES pokemon (id),
    min_level           INTEGER NOT NULL,
    max_level           INTEGER NOT NULL
);

-- Feebas-class answers ("only under this condition, only while surfing") need
-- this join — PLAN.md §0.6.3.
CREATE TABLE encounter_condition_value_map (
    encounter_id                    INTEGER NOT NULL REFERENCES encounters (id),
    encounter_condition_value_id    INTEGER NOT NULL
        REFERENCES encounter_condition_values (id),
    PRIMARY KEY (encounter_id, encounter_condition_value_id)
);

CREATE TABLE location_area_encounter_rates (
    location_area_id    INTEGER NOT NULL REFERENCES location_areas (id),
    encounter_method_id INTEGER NOT NULL REFERENCES encounter_methods (id),
    version_id          INTEGER NOT NULL REFERENCES versions (id),
    rate                INTEGER,
    PRIMARY KEY (location_area_id, encounter_method_id, version_id)
);


-- ---------------------------------------------------------------------------
-- Showdown cross-check (PLAN.md §1.2.2, §3.6)
-- ---------------------------------------------------------------------------
--
-- Deliberately a **separate table**, never merged into `pokemon_moves`.
-- Showdown has complete learnsets but no TM numbers and no location data, so
-- PokéAPI stays primary; where the two disagree the tool surfaces the
-- discrepancy rather than silently picking a winner. Merging would destroy
-- exactly the signal this table exists to provide.
--
-- `pokemon_id` / `move_id` hold the resolved PokéAPI ids, or NULL when the
-- normalized Showdown key matched nothing. They carry no foreign key on
-- purpose: "unmatched" has to be a queryable outcome rather than a load-time
-- abort, and the build reports the match rate so a silent 0% join cannot pass
-- for a healthy cross-check.
CREATE TABLE showdown_learnset (
    species_key     TEXT NOT NULL,      -- Showdown species id, e.g. "mrmime"
    move_key        TEXT NOT NULL,      -- Showdown move id, e.g. "solarbeam"
    generation      INTEGER NOT NULL,   -- leading digit of the code: 4 in "4M"
    method          TEXT NOT NULL,      -- letter of the code: L E M T S D V R
    level           INTEGER,            -- trailing digits of an "L" code
    raw_code        TEXT NOT NULL,      -- untouched source code, e.g. "9L25"
    pokemon_id      INTEGER,            -- resolved PokéAPI pokemon.id, or NULL
    move_id         INTEGER,            -- resolved PokéAPI moves.id, or NULL
    PRIMARY KEY (species_key, move_key, raw_code)
);


-- ---------------------------------------------------------------------------
-- Tier 2 cache (PLAN.md §1.2.3) — created now so the schema is stable for
-- Phase 3 and a rebuild needs no migration.
-- ---------------------------------------------------------------------------
--
-- `payload` is always JSON. PLAN.md §6.7: wikitext must never cross the tool
-- boundary, and caching it here would put that violation one careless SELECT
-- away.
--
-- `expires_at` NULL means "never" — section *indexes* are cached indefinitely
-- (PLAN.md §0.1: names are stable, indexes are not, so what is cached is the
-- resolved name→index mapping). Section *content* gets a 30-day expiry.
CREATE TABLE wiki_cache (
    cache_key       TEXT PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('sections', 'content', 'page')),
    page_title      TEXT NOT NULL,
    section_name    TEXT,
    payload         TEXT NOT NULL,
    fetched_at      TEXT NOT NULL,      -- ISO-8601 UTC
    expires_at      TEXT                -- ISO-8601 UTC; NULL = never expires
);


-- ---------------------------------------------------------------------------
-- Build provenance
-- ---------------------------------------------------------------------------

CREATE TABLE meta (
    key     TEXT PRIMARY KEY,
    value   TEXT
);

-- Per-file provenance, so `poke-tools-build --refresh` can name *which* upstream
-- file moved rather than only reporting that the aggregate hash changed.
CREATE TABLE ingest_source_files (
    source          TEXT NOT NULL,      -- 'pokeapi-csv' | 'showdown'
    filename        TEXT NOT NULL,
    url             TEXT NOT NULL,
    etag            TEXT,
    content_sha256  TEXT NOT NULL,
    byte_size       INTEGER NOT NULL,
    row_count       INTEGER,
    PRIMARY KEY (source, filename)
);


-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------

-- PLAN.md §6.5, and the single most load-bearing line in this file: this
-- composite carries essentially every learnset query. It is genuinely
-- additional rather than a redundant prefix of the primary key — the PK leads
-- with (pokemon_id, version_group_id, move_id), which cannot serve a
-- method-filtered lookup.
CREATE INDEX idx_pokemon_moves_learnset
    ON pokemon_moves (pokemon_id, version_group_id, pokemon_move_method_id);

-- "Who, in this game, learns move X" — the reverse direction, for team_check
-- and can_learn's batch path.
CREATE INDEX idx_pokemon_moves_by_move
    ON pokemon_moves (version_group_id, move_id, pokemon_move_method_id);

-- Encounter path, keyed on version_id per PLAN.md §0.6.3.
CREATE INDEX idx_encounters_version_pokemon
    ON encounters (version_id, pokemon_id);
CREATE INDEX idx_encounters_version_area
    ON encounters (version_id, location_area_id);
CREATE INDEX idx_encounters_pokemon
    ON encounters (pokemon_id);
CREATE INDEX idx_encounters_slot
    ON encounters (encounter_slot_id);
CREATE INDEX idx_encounter_slots_vg_method
    ON encounter_slots (version_group_id, encounter_method_id);
CREATE INDEX idx_location_areas_location
    ON location_areas (location_id);

CREATE INDEX idx_machines_vg_move
    ON machines (version_group_id, move_id);

-- get_item's two per-item lookups: "does this item exist in generation G" and
-- "who holds it in these versions".
CREATE INDEX idx_item_game_indices_item
    ON item_game_indices (item_id, generation_id);
CREATE INDEX idx_pokemon_items_item
    ON pokemon_items (item_id, version_id);

CREATE INDEX idx_move_changelog_move
    ON move_changelog (move_id, changed_in_version_group_id);

-- The `*_past` overlays are always read as "smallest generation_id >= target",
-- which is a range scan the primary keys already lead with — no extra index
-- needed. Named here so the absence is a decision rather than an oversight.

-- Name resolution for the fuzzy find_pokemon / move / item lookups.
CREATE UNIQUE INDEX idx_pokemon_species_identifier ON pokemon_species (identifier);
CREATE UNIQUE INDEX idx_pokemon_identifier ON pokemon (identifier);
CREATE UNIQUE INDEX idx_moves_identifier ON moves (identifier);
-- Not unique, unlike the three above: upstream ships `roseli-berry` twice
-- (item ids 723 and 2279, differing only in cost). `get_item` therefore has to
-- treat identifier lookup as potentially multi-row rather than assume a single
-- hit.
CREATE INDEX idx_items_identifier ON items (identifier);
CREATE INDEX idx_pokemon_species_names_name ON pokemon_species_names (name);
CREATE INDEX idx_move_names_name ON move_names (name);
CREATE INDEX idx_item_names_name ON item_names (name);
CREATE INDEX idx_ability_names_name ON ability_names (name);

CREATE INDEX idx_pokemon_species_evolves_from ON pokemon_species (evolves_from_species_id);
CREATE INDEX idx_pokemon_species_chain ON pokemon_species (evolution_chain_id);
CREATE INDEX idx_pokemon_species_id ON pokemon (species_id);
CREATE INDEX idx_pokemon_evolution_species ON pokemon_evolution (evolved_species_id);
CREATE INDEX idx_pokemon_egg_groups_group ON pokemon_egg_groups (egg_group_id);
CREATE INDEX idx_pokemon_types_type ON pokemon_types (type_id);

CREATE INDEX idx_showdown_learnset_pokemon
    ON showdown_learnset (pokemon_id, generation, method);
CREATE INDEX idx_showdown_learnset_move
    ON showdown_learnset (move_id, generation);

CREATE INDEX idx_wiki_cache_expiry ON wiki_cache (expires_at);
