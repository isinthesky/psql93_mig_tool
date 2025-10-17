
## partition_table_info

-- public.partition_table_info definition

-- Drop table

-- DROP TABLE public.partition_table_info;

CREATE TABLE public.partition_table_info (
	table_name varchar(100) NOT NULL,
	table_data varchar(10) NOT NULL,
	from_date int8 NOT NULL,
	to_date int8 NOT NULL,
	use_flag bool NOT NULL,
	save_date timestamp NOT NULL,
	cluster_index bool DEFAULT false NULL
);

---

point_history_240920	PH	1726758000000	1726844399999	true	2024-09-19 00:30:00.075	true
point_history_240921	PH	1726844400000	1726930799999	true	2024-09-20 00:30:00.025	true
point_history_240922	PH	1726930800000	1727017199999	true	2024-09-21 00:30:00.054	true
trend_history_2002	TH	1580482800000	1582988399999	true	2020-01-01 00:30:00.616	true
trend_history_1808	TH	1533049200000	1535727599999	true	2018-07-01 00:30:00.356	true
energy_display_2002	ED	1580482800000	1582988399999	true	2020-01-01 00:30:00.467	true
trend_history_2402	TH	1706713200000	1709218799999	true	2024-01-01 00:30:00.917	true
point_history_240923	PH	1727017200000	1727103599999	true	2024-09-22 00:30:00.110	true
energy_display_2402	ED	1706713200000	1709218799999	true	2024-01-01 00:30:00.696	true
running_time_history_2106	RT	1622473200000	1625065199999	true	2021-05-01 00:30:00.113	false
point_history_240924	PH	1727103600000	1727189999999	true	2024-09-23 00:30:00.019	true
point_history_240925	PH	1727190000000	1727276399999	true	2024-09-24 00:30:00.017	true
point_history_240926	PH	1727276400000	1727362799999	true	2024-09-25 00:30:00.056	true
point_history_240927	PH	1727362800000	1727449199999	true	2024-09-26 00:30:00.060	true
point_history_240928	PH	1727449200000	1727535599999	true	2024-09-27 00:30:00.034	true


---

## point_history

-- public.point_history definition

-- Drop table

-- DROP TABLE public.point_history;

CREATE TABLE public.point_history (
	path_id int8 NOT NULL,
	issued_date int8 NOT NULL,
	changed_value varchar(100) NULL,
	connection_status bool NULL
);
CREATE INDEX point_history_path_id_date ON public.point_history USING btree (path_id, issued_date);
CREATE INDEX point_history_path_id_idx ON public.point_history USING btree (path_id);

---


64435	1702566360000	23.347	true
64435	1702566420000	23.366	true
64435	1702566480000	23.372	true
64435	1702566540000	23.382	true
64435	1702566600000	23.348	true
64435	1702566660000	23.344	true
64435	1702566720000	23.342	true
64435	1702566780000	23.372	true
64435	1702566840000	23.368	true
64435	1702566900000	23.388	true
64435	1702566960000	23.416	true
64435	1702567020000	23.413	true
64435	1702567080000	23.414	true
64435	1702567140000	23.413	true
64435	1702567200000	23.39	true
64435	1702567260000	23.406	true
64435	1702567320000	23.414	true
64435	1702567380000	23.442	true
64435	1702567440000	23.405	true
64435	1702567500000	23.419	true
64435	1702567560000	23.428	true
64435	1702567620000	23.408	true
64435	1702567680000	23.396	true
64435	1702567740000	23.39	true
64435	1702567800000	23.41	true
64435	1702567860000	23.363	true
64435	1702567920000	23.39	true
64435	1702567980000	23.383	true
64435	1702568040000	23.408	true
64435	1702568100000	23.434	true
64435	1702568160000	23.432	true
64435	1702568220000	23.413	true
64435	1702568280000	23.425	true
64435	1702568340000	23.418	true
64435	1702568400000	23.42	true
64435	1702568460000	23.438	true
64435	1702568520000	23.461	true
64435	1702568580000	23.47	true
64435	1702568640000	23.471	true
64435	1702568700000	23.447	true
64435	1702568760000	23.434	true
64435	1702568820000	23.471	true
64435	1702568880000	23.448	true
64435	1702568940000	23.489	true
64435	1702569000000	23.48	true
64435	1702569060000	23.454	true
64435	1702569120000	23.48	true
64435	1702569180000	23.462	true
64435	1702569240000	23.453	true
64435	1702569300000	23.465	true
64435	1702569360000	23.49	true
64435	1702569420000	23.506	true


---

## point_history_{YYMMDD}

-- public.point_history_231216 definition

-- Drop table

-- DROP TABLE public.point_history_231216;

CREATE TABLE public.point_history_231216 (
	CONSTRAINT point_history_231216_issued_date_check CHECK (((issued_date >= 1702652400000::bigint) AND (issued_date <= 1702738799999::bigint)))
)
INHERITS (public.point_history);

---

64435	1702652520000	23.174	true
64435	1702652580000	23.142	true
64435	1702652640000	23.146	true
64435	1702652700000	23.11	true
64435	1702652760000	23.128	true
64435	1702652820000	23.141	true
64435	1702652880000	23.138	true
64435	1702652940000	23.107	true
64435	1702653000000	23.126	true
64435	1702653060000	23.143	true

---

## point_master

-- public.point_master definition

-- Drop table

-- DROP TABLE public.point_master;

CREATE TABLE public.point_master (
	point_path varchar(128) NOT NULL,
	station_id varchar(10) NOT NULL,
	path_id int8 NULL,
	use bool DEFAULT true NULL
);


## db functions

-- DROP FUNCTION public.drop_partition_table(text);

CREATE OR REPLACE FUNCTION public.drop_partition_table(text)
 RETURNS integer
 LANGUAGE plpgsql
AS $function$
DECLARE

data record;
rtval integer;

BEGIN
EXECUTE 'DROP TABLE IF EXISTS '||$1||' CASCADE;'
	'DELETE FROM partition_table_info WHERE table_name='''||$1||''';';
--	'UPDATE partition_table_info SET use_flag=''N'', save_date=NOW() WHERE table_name='''||$1||''';';
	
rtval = 1;
RETURN rtval;
END;

$function$
;

--- 

-- DROP FUNCTION public.energy_display_pt(text, text, int8, int8, timestamp, timestamp);

CREATE OR REPLACE FUNCTION public.energy_display_pt(text, text, bigint, bigint, timestamp without time zone, timestamp without time zone)
 RETURNS integer
 LANGUAGE plpgsql
AS $function$
DECLARE

data record;
rtval integer;

BEGIN
EXECUTE 'SELECT tablename from pg_tables where tablename=''energy_display_'||$1||''' AND schemaname=''public''' INTO data;

IF data.tablename is null THEN
	EXECUTE 'CREATE TABLE energy_display_'||$1||' (PRIMARY KEY(sensor_id, issued_date), CHECK(issued_date >= '''||$5||''' AND issued_date <= '''||$6||''')) INHERITS (energy_display);
	CREATE RULE rule_energy_display_'||$1||' AS ON INSERT TO energy_display WHERE (issued_date >= '''||$5||''' AND issued_date <= '''||$6||''') DO INSTEAD 
	INSERT INTO energy_display_'||$1||' VALUES(NEW.*);
	INSERT INTO partition_table_info VALUES(''energy_display_'||$1||''', '''||$2||''', '||$3||', '||$4||', ''Y'', NOW());
	CLUSTER energy_display_'||$1||' USING energy_display_'||$1||'_pkey;';
	rtval = 1;
ELSE
	rtval = 0;
END IF;

RETURN rtval;
END;

$function$
;

---

-- DROP FUNCTION public.get_point_history_tz_offset();

CREATE OR REPLACE FUNCTION public.get_point_history_tz_offset()
 RETURNS bigint
 LANGUAGE sql
AS $function$
 SELECT CAST(
	((60*60*24) - (extract(epoch from (select utc_offset from pg_timezone_names WHERE name=(SELECT  current_setting('TIMEZONE'))))))*1000 as bigint
	)
$function$
;

---

-- DROP FUNCTION public.point_history_partition_insert();

CREATE OR REPLACE FUNCTION public.point_history_partition_insert()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
DECLARE
_insert_time bigint;
_insert_date text;
 
BEGIN
    _insert_time := (NEW.issued_date/1000)::bigint; -- conversion time from milliseconds to seconds
    _insert_date := to_char(to_timestamp(_insert_time), 'YYMMDD');
 
    EXECUTE  'INSERT INTO point_history_'||_insert_date||' VALUES ($1.*);' USING NEW;
 
    RETURN NULL;
END;

$function$
;

--- 

-- DROP FUNCTION public.point_history_pt(text, text, int8, int8);

CREATE OR REPLACE FUNCTION public.point_history_pt(text, text, bigint, bigint)
 RETURNS integer
 LANGUAGE plpgsql
AS $function$
DECLARE

data record;
rtval integer;

BEGIN
EXECUTE 'SELECT tablename from pg_tables where tablename=''point_history_'||$1||''' AND schemaname=''public''' INTO data;

IF data.tablename is null THEN
	EXECUTE 'CREATE TABLE point_history_'||$1||' (PRIMARY KEY(path_id, issued_date), CHECK(issued_date >= '||$3||' AND issued_date <= '||$4||')) INHERITS (point_history);
	INSERT INTO partition_table_info VALUES(''point_history_'||$1||''', '''||$2||''', '||$3||', '||$4||', ''Y'', NOW());
	CLUSTER point_history_'||$1||' USING point_history_'||$1||'_pkey;';
	rtval = 1;
ELSE
	rtval = 0;
END IF;

RETURN rtval;
END;

$function$
;

---

-- DROP FUNCTION public.point_sec_history_partition_insert();

CREATE OR REPLACE FUNCTION public.point_sec_history_partition_insert()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
DECLARE
_insert_time bigint;
_insert_date text;
 
BEGIN
    _insert_time := (NEW.issued_date/1000)::bigint; -- conversion time from milliseconds to seconds
    _insert_date := to_char(to_timestamp(_insert_time), 'YYMMDD');
 
    EXECUTE  'INSERT INTO point_sec_history_'||_insert_date||' VALUES ($1.*);' USING NEW;
 
    RETURN NULL;
END;

$function$
;

---

-- DROP FUNCTION public.point_sec_history_pt(text, text, int8, int8);

CREATE OR REPLACE FUNCTION public.point_sec_history_pt(text, text, bigint, bigint)
 RETURNS integer
 LANGUAGE plpgsql
AS $function$
DECLARE

data record;
rtval integer;

BEGIN
EXECUTE 'SELECT tablename from pg_tables where tablename=''point_sec_history_'||$1||''' AND schemaname=''public''' INTO data;

IF data.tablename is null THEN
	EXECUTE 'CREATE TABLE point_sec_history_'||$1||' (PRIMARY KEY(path_id, issued_date), CHECK(issued_date >= '||$3||' AND issued_date <= '||$4||')) INHERITS (point_sec_history);
	INSERT INTO partition_table_info VALUES(''point_sec_history_'||$1||''', '''||$2||''', '||$3||', '||$4||', ''Y'', NOW());
	CLUSTER point_sec_history_'||$1||' USING point_sec_history_'||$1||'_pkey;';
	rtval = 1;
ELSE
	rtval = 0;
END IF;

RETURN rtval;
END;

$function$
;

---

-- DROP FUNCTION public.running_time_history_pt(text, text, int8, int8);

CREATE OR REPLACE FUNCTION public.running_time_history_pt(text, text, bigint, bigint)
 RETURNS integer
 LANGUAGE plpgsql
AS $function$
DECLARE

data record;
rtval integer;

BEGIN
EXECUTE 'SELECT tablename from pg_tables where tablename=''running_time_history_'||$1||''' AND schemaname=''public''' INTO data;

IF data.tablename is null THEN
	EXECUTE 'CREATE TABLE running_time_history_'||$1||' (PRIMARY KEY(path_id, issued_date, save_type), CHECK(issued_date >= '||$3||' AND issued_date <= '||$4||')) INHERITS (running_time_history);
	CREATE RULE rule_running_time_history_'||$1||' AS ON INSERT TO running_time_history WHERE (issued_date >= '||$3||' AND issued_date <= '||$4||') DO INSTEAD 
	INSERT INTO running_time_history_'||$1||' VALUES(NEW.*);
	CREATE INDEX running_time_history_'||$1||'_idx ON running_time_history_'||$1||' USING btree (path_id, issued_date);
	INSERT INTO partition_table_info VALUES(''running_time_history_'||$1||''', '''||$2||''', '||$3||', '||$4||', ''Y'', NOW());';
	rtval = 1;
ELSE
	rtval = 0;
END IF;

RETURN rtval;
END;

$function$
;

---

-- DROP FUNCTION public.trend_history_pt(text, text, int8, int8);

CREATE OR REPLACE FUNCTION public.trend_history_pt(text, text, bigint, bigint)
 RETURNS integer
 LANGUAGE plpgsql
AS $function$
DECLARE

data record;
rtval integer;

BEGIN
EXECUTE 'SELECT tablename from pg_tables where tablename=''trend_history_'||$1||''' AND schemaname=''public''' INTO data;

IF data.tablename is null THEN
	EXECUTE 'CREATE TABLE trend_history_'||$1||' (PRIMARY KEY(path_id, issued_date), CHECK(issued_date >= '||$3||' AND issued_date <= '||$4||')) INHERITS (trend_history);
	CREATE RULE rule_trend_history_'||$1||' AS ON INSERT TO trend_history WHERE (issued_date >= '||$3||' AND issued_date <= '||$4||') DO INSTEAD 
	INSERT INTO trend_history_'||$1||' VALUES(NEW.*);
	INSERT INTO partition_table_info VALUES(''trend_history_'||$1||''', '''||$2||''', '||$3||', '||$4||', ''Y'', NOW());
	CLUSTER trend_history_'||$1||' USING trend_history_'||$1||'_pkey;';
	rtval = 1;
ELSE
	rtval = 0;
END IF;

RETURN rtval;
END;

$function$
;

