# PROD DB Corruption Recovery Protocol — 2026-09-05

**Status: OPEN — verification pending PROD restart on the recovered database.**

## Incident summary

PROD's `qapbot.db` (WAL mode) was corrupted on 2026-09-05 when read-only SQLite queries were
run against it over the SMB network share while the bot was live, causing PROD's own process to
hang for ~69 minutes and a WAL checkpoint to complete incoherently. Root cause, mechanism, and
the standing rule against this are documented in `.github/copilot-instructions.md` (Cardinal
Rule 18) and `qapbot/docs/COPILOT_PITFALLS_COOKBOOK.md` (Pitfall 62) — this file does not repeat
that; it exists solely to track recovery of the affected data.

The database was recovered via `sqlite3 .recover` (official 3.53.4 build) into
`qapbot_recovered.db`: `PRAGMA integrity_check` = `ok`, zero duplicate primary keys, zero rows in
`lost_and_found`, all 47 tables present. The recovery's `war_summary`/`war_attacks` cutoff is
**2026-09-05 12:29:44 local** (10:29:44 UTC) — everything after that, up to the crash at 13:38:54
local, is genuinely absent from the recovered DB.

## What this file is for

The 210 wars below were finalized (temp file processed, logged) in that final window but never
reached the recovered database. All 210 are now sitting in `data/temp/` on PROD (58 of them were
already moved to `archive/` by the finalize step before the crash and were restored back to
`temp/` — copy-verified via SHA-256 before the archive originals were deleted). Once PROD runs on
the recovered/restored database, its normal, unmodified finalize logic
(`manage_war_files` → `_process_war_history`) should pick every one of these up on its own —
no custom script, no manual DB write. This file is the checklist to confirm that actually
happened, so nothing from this list silently falls through a second time.

## Summary

- Total wars at risk: **210**
- Already in `temp/` at crash time (no action taken): **152**
- Restored from `archive/` back to `temp/` (copy-verified, hash-checked): **58**
- CWL wars: **186** / 210
- State at capture: 209 `war_ended`, 1 `in_war` (an orphan-regular finalize)
- All 210 temp files JSON-validated after the restore: **210/210 parse cleanly**

## Verification procedure (run after PROD has processed at least one full cycle)

Once PROD is back up on the recovered database and has run a cycle, confirm every war below
landed in `war_summary`. Do this from a **local copy of the DB** (synced to DEV), never by
opening PROD's live database over the network share (Cardinal Rule 18).

```sql
-- for each (clan_tag, war_id) pair below:
SELECT 1 FROM war_summary WHERE clan_tag = '#<CLAN>' AND war_id = '<WAR_ID>';
```

Expected: all 210 return exactly one row. Any that don't are a second, independent loss and
need investigation (check the clan's `data/temp/` shard for the file first — it may simply not
have been processed yet if that clan hasn't had a war-check cycle).

| # | clan_tag | war_id | category |
|---|---|---|---|
| 1 | #20LCPJ9GR | 2JQQGYU02_202609040928 | restored_from_archive |
| 2 | #22CLQG9PV | 28CUG2Q2_202609040922 | restored_from_archive |
| 3 | #28RPLQ299 | 2U8RR2RJQ_202609040924 | restored_from_archive |
| 4 | #28UJPLQR2 | QUCR2UV0_202609040922 | restored_from_archive |
| 5 | #29RGC8G29 | 2J880GY9R_202609040924 | restored_from_archive |
| 6 | #2G8GVJQP2 | 2RRQP2U20_202609040922 | restored_from_archive |
| 7 | #2G9LG9GPJ | 8GCQLQP_202609040925 | restored_from_archive |
| 8 | #2GC0J2QUQ | C9VPRPU8_202609040926 | restored_from_archive |
| 9 | #2GCRCU2L0 | 2Q0R2GQGR_202609040923 | restored_from_archive |
| 10 | #2GJ92882P | VR2VQPCY_202609040922 | restored_from_archive |
| 11 | #2GJ9P8YRL | 2G9PP0GV2_202609040922 | restored_from_archive |
| 12 | #2GPPCQQV8 | 2JRUC9Q2Y_202609040927 | restored_from_archive |
| 13 | #2GQCJCQY | 20P00UPLL_202609040922 | restored_from_archive |
| 14 | #2GRCJ0J2L | PGVP8JQG_202609040927 | restored_from_archive |
| 15 | #2J8VUPGQL | 2LR8822V_202609040928 | restored_from_archive |
| 16 | #2JG8U88JY | 2RYYUY9YP_202609040923 | restored_from_archive |
| 17 | #2JQJURYJY | 2GP9LLY8U_202609040925 | restored_from_archive |
| 18 | #2JR0RC90P | V9GYPUG0_202609040926 | restored_from_archive |
| 19 | #2JY9PGU0R | 2RUQUQ0CQ_202609040923 | restored_from_archive |
| 20 | #2L00Y9J8Y | 2YGGU22JV_202609040927 | restored_from_archive |
| 21 | #2L8V80U99 | 2JGLYG_202609040924 | restored_from_archive |
| 22 | #2LCPU9CV9 | 2JLPQ8U2Y_202609040928 | restored_from_archive |
| 23 | #2LGLRVLQU | 2Q8LUV9CY_202609040924 | restored_from_archive |
| 24 | #2LRPLJV0Y | 89PQUCQL_202609040925 | restored_from_archive |
| 25 | #2P2RV0YJ9 | 2G20VC0C_202609040923 | restored_from_archive |
| 26 | #2P2YVV92U | 2JY2P9VRU_202609040926 | restored_from_archive |
| 27 | #2PJPCVYGY | 2UVP2U9V_202609040927 | restored_from_archive |
| 28 | #2Q0JQ2UQJ | 2YV2JRYVR_202609040923 | restored_from_archive |
| 29 | #2Q0R2GQGR | 2GCRCU2L0_202609040923 | restored_from_archive |
| 30 | #2QJQUQP2L | 298L9YVLU_202609040928 | restored_from_archive |
| 31 | #2QQGURRV9 | 28JGPYPU2_202609040928 | restored_from_archive |
| 32 | #2QQLCPPPY | YJCUPQRC_202609040929 | restored_from_archive |
| 33 | #2QVC22LPC | 2JRU20VQ2_202609040925 | restored_from_archive |
| 34 | #2RUQUQ0CQ | 2JY9PGU0R_202609040923 | restored_from_archive |
| 35 | #2UVP2U9V | 2PJPCVYGY_202609040927 | restored_from_archive |
| 36 | #880CLPCJ | YGL80JU2_202609040925 | restored_from_archive |
| 37 | #89LQVJU2 | Q0YUUQGR_202609040923 | restored_from_archive |
| 38 | #89PQUCQL | 2LRPLJV0Y_202609040925 | restored_from_archive |
| 39 | #8CJU929Q | 2PUQVPG9C_202609040925 | restored_from_archive |
| 40 | #8G2V9RGG | 299RRL02U_202609040923 | restored_from_archive |
| 41 | #9QR9L0G9 | JYUVRP2R_202609040928 | restored_from_archive |
| 42 | #9URP88R8 | 2UPPC2P9Y_202609040925 | restored_from_archive |
| 43 | #9UUCGRVG | 2GUJYLL8J_202609040925 | restored_from_archive |
| 44 | #C9VPRPU8 | 2GC0J2QUQ_202609040926 | restored_from_archive |
| 45 | #J2CUGJPC | 20V0P0CG0_202609040925 | restored_from_archive |
| 46 | #JP2ULJQC | 8PQP8RR0_202609040929 | restored_from_archive |
| 47 | #LRG2RYU2 | 9Q8U0Q8V_202609040926 | restored_from_archive |
| 48 | #P022YCPJ | 2PQ8VVGQL_202609040922 | restored_from_archive |
| 49 | #QUCR2UV0 | 28UJPLQR2_202609040922 | restored_from_archive |
| 50 | #QY9GGQPQ | 2G9J2C9RJ_202609040925 | restored_from_archive |
| 51 | #QYGQPQQ | 2GRV9J8JL_202609040926 | restored_from_archive |
| 52 | #R8RCJ9PY | 2LUVGY88Y_202609040925 | restored_from_archive |
| 53 | #RGVVGQPR | YPG2J0LG_202609040925 | restored_from_archive |
| 54 | #Y0GQRCYP | 2QR0L2RYR_202609040928 | restored_from_archive |
| 55 | #Y2P8Y2QP | 2G9VPRJJY_202609040926 | restored_from_archive |
| 56 | #Y2RRC9UY | Q2PUYJVJ_202609040926 | restored_from_archive |
| 57 | #YU9RPGGG | PUU8RY88_202609040925 | restored_from_archive |
| 58 | #YYVQQYC | Q08R9V9U_202609040922 | restored_from_archive |
| 59 | #209QRU22 | 2GJUYG9VU_202609041024 | was_already_in_temp |
| 60 | #20G8Q8VJ9 | LQPU2YUL_202609041027 | was_already_in_temp |
| 61 | #20LV29JJQ | 2LGY9QC9J_202609041025 | was_already_in_temp |
| 62 | #20PLQJJRC | 2R8GLPUC0_202609041027 | was_already_in_temp |
| 63 | #22LV9CGG8 | 8P0Y2CU_202609041028 | was_already_in_temp |
| 64 | #22YJVJRC9 | RQ9C2QU8_202609041024 | was_already_in_temp |
| 65 | #28892QQVR | 2CG99LYVC_202609041027 | was_already_in_temp |
| 66 | #28G8URLV2 | 2PRQL28Y9_202609041025 | was_already_in_temp |
| 67 | #28RJRQ82Y | 8CGYRC8J_202609041024 | was_already_in_temp |
| 68 | #28V8RGUVG | 29V02PJ0Y_202609041027 | was_already_in_temp |
| 69 | #2990CPLJ | R2LPV2VP_202609041021 | was_already_in_temp |
| 70 | #29JJRPR0J | 2J8JGR2PC_202609041025 | was_already_in_temp |
| 71 | #29PPJUJYP | 8G9JQGV0_202609041021 | was_already_in_temp |
| 72 | #29V02PJ0Y | 28V8RGUVG_202609041027 | was_already_in_temp |
| 73 | #29V2GUV | 2GUY98PU_202609041025 | was_already_in_temp |
| 74 | #2C0PQLU8V | 2RL9R02LV_202609041024 | was_already_in_temp |
| 75 | #2CLCPR9J8 | JRPL08Q_202609041020 | was_already_in_temp |
| 76 | #2G082PL0P | RGY9QYQC_202609041021 | was_already_in_temp |
| 77 | #2G88R9LU8 | YLV8YJRP_202609041025 | was_already_in_temp |
| 78 | #2G8PJCPJV | J0QLRVLU_202609041024 | was_already_in_temp |
| 79 | #2G8Q0V8JJ | 92VR9GYP_202609041024 | was_already_in_temp |
| 80 | #2G99U98RQ | 8VCGJ2VP_202609041023 | was_already_in_temp |
| 81 | #2G9J9J909 | 2LQ0YP8VV_202609041023 | was_already_in_temp |
| 82 | #2GJUYG9VU | 209QRU22_202609041024 | was_already_in_temp |
| 83 | #2GPLY2CRP | 2LUCJG0YP_202609041023 | was_already_in_temp |
| 84 | #2GQ0P8PJC | 2Y9GLQGRP_202609041025 | was_already_in_temp |
| 85 | #2GUY98PU | 29V2GUV_202609041025 | was_already_in_temp |
| 86 | #2J2JV89CJ | 2JCGLLUQQ_202609041022 | was_already_in_temp |
| 87 | #2J89Y28L0 | PG29VCL0_202609041025 | was_already_in_temp |
| 88 | #2J8JGR2PC | 29JJRPR0J_202609041025 | was_already_in_temp |
| 89 | #2J9RJJQYL | 2RYPVLPJC_202609041024 | was_already_in_temp |
| 90 | #2J9UG220G | G9PC2L00_202609041021 | was_already_in_temp |
| 91 | #2JGGGYJQR | YCJUP882_202609041022 | was_already_in_temp |
| 92 | #2JGU9YJPP | 2QPJYP9Y2_202609041024 | was_already_in_temp |
| 93 | #2JJCUR9GC | 2R9G82QJU_202609041028 | was_already_in_temp |
| 94 | #2JJJJJJJ0 | 2JC8VL0C2_202609041017 | was_already_in_temp |
| 95 | #2JPC88R8C | 2RQGULPU9_202609041028 | was_already_in_temp |
| 96 | #2JPP8RJY2 | 2Q8GVJP9Y_202609041028 | was_already_in_temp |
| 97 | #2JQR8V8C | 2PP22UQ90_202609041028 | was_already_in_temp |
| 98 | #2JR99C8JJ | 2V9J80V0_202609041028 | was_already_in_temp |
| 99 | #2JVVGRYUG | 2LCGCGYL8_202609041027 | was_already_in_temp |
| 100 | #2JYVCV2VJ | 2JPPR9CGQ_202609041020 | was_already_in_temp |
| 101 | #2L8CY08Y8 | 2RR99JGGG_202609041025 | was_already_in_temp |
| 102 | #2L9JLG0VQ | P2GYJ908_202609041022 | was_already_in_temp |
| 103 | #2LCGCGYL8 | 2JVVGRYUG_202609041027 | was_already_in_temp |
| 104 | #2LGY9QC9J | 20LV29JJQ_202609041025 | was_already_in_temp |
| 105 | #2LL8YVRRY | PQGRGRY0_202609041024 | was_already_in_temp |
| 106 | #2LL92U2JJ | 89GQCQUU_202609041023 | was_already_in_temp |
| 107 | #2LQ0V0CL | UYPUQ8PY_202609041023 | was_already_in_temp |
| 108 | #2LULQRCVJ | 2JC2J9PCY_202609041025 | was_already_in_temp |
| 109 | #2LV8RYYL | 2YLJVVU2Y_202609041024 | was_already_in_temp |
| 110 | #2LYL9CGCQ | UGQRUR00_202609041028 | was_already_in_temp |
| 111 | #2PGGUL2YY | 2C9JPPQV9_202609041028 | was_already_in_temp |
| 112 | #2PP22UQ90 | 2JQR8V8C_202609041028 | was_already_in_temp |
| 113 | #2PRQL28Y9 | 28G8URLV2_202609041025 | was_already_in_temp |
| 114 | #2PULP2CCC | 2Q2VP9GUJ_202609041020 | was_already_in_temp |
| 115 | #2PY82PJP | V9PGJCL8_202609041020 | was_already_in_temp |
| 116 | #2Q2LJ8LLQ | VCGPYLLR_202609041028 | was_already_in_temp |
| 117 | #2Q2VP9GUJ | 2PULP2CCC_202609041020 | was_already_in_temp |
| 118 | #2Q8GVJP9Y | 2JPP8RJY2_202609041028 | was_already_in_temp |
| 119 | #2QLJLVCP8 | UVJLGU0G_202609041024 | was_already_in_temp |
| 120 | #2QPJR90LV | G29QQ8G8_202609041021 | was_already_in_temp |
| 121 | #2QPJYP9Y2 | 2JGU9YJPP_202609041024 | was_already_in_temp |
| 122 | #2QQC89YP0 | 2R0Q9VP_202609041028 | was_already_in_temp |
| 123 | #2R0GQ0Q29 | 9Q0CJCVQ_202609041021 | was_already_in_temp |
| 124 | #2R0Q9VP | 2QQC89YP0_202609041028 | was_already_in_temp |
| 125 | #2R0RV988Q | Y9VP0VR0_202609041023 | was_already_in_temp |
| 126 | #2R8GLPUC0 | 20PLQJJRC_202609041027 | was_already_in_temp |
| 127 | #2R9G82QJU | 2JJCUR9GC_202609041028 | was_already_in_temp |
| 128 | #2RL09QCG0 | 2JCJPU8LV_202609041019 | was_already_in_temp |
| 129 | #2RLP9L8GU | GQ8YL09G_202609041021 | was_already_in_temp |
| 130 | #2RQGULPU9 | 2JPC88R8C_202609041028 | was_already_in_temp |
| 131 | #2RR99JGGG | 2L8CY08Y8_202609041025 | was_already_in_temp |
| 132 | #2RUGQPUPJ | 2RV2U80GL_202609041028 | was_already_in_temp |
| 133 | #2RULJ2CRY | LPCL80JQ_202609041028 | was_already_in_temp |
| 134 | #2RUV980UQ | QQV9J90G_202609041027 | was_already_in_temp |
| 135 | #2RYPVLPJC | 2J9RJJQYL_202609041024 | was_already_in_temp |
| 136 | #2U0G8GR2 | 2RR80JQYJ_202609041023 | was_already_in_temp |
| 137 | #2U0PLVQJR | GRGVPLUV_202609041021 | was_already_in_temp |
| 138 | #2V9J80V0 | 2JR99C8JJ_202609041028 | was_already_in_temp |
| 139 | #2Y9GLQGRP | 2GQ0P8PJC_202609041025 | was_already_in_temp |
| 140 | #2YG8LJPYC | 2CGV8G0GG_202609041020 | was_already_in_temp |
| 141 | #2YLJVVU2Y | 2LV8RYYL_202609041024 | was_already_in_temp |
| 142 | #2YYPVL0G | GP9PYVL_202609041028 | was_already_in_temp |
| 143 | #80U9U8P8 | GR8R0UG2_202609041025 | was_already_in_temp |
| 144 | #88222LQJ | 8G8JC9CQ_202609041024 | was_already_in_temp |
| 145 | #89GQCQUU | 2LL92U2JJ_202609041023 | was_already_in_temp |
| 146 | #8CGYRC8J | 28RJRQ82Y_202609041024 | was_already_in_temp |
| 147 | #8CLRLJC0 | 8YJU29PY_202609041021 | was_already_in_temp |
| 148 | #8G8JC9CQ | 88222LQJ_202609041024 | was_already_in_temp |
| 149 | #8G9JQGV0 | 29PPJUJYP_202609041021 | was_already_in_temp |
| 150 | #8P0Y2CU | 22LV9CGG8_202609041028 | was_already_in_temp |
| 151 | #8QVVG0Q2 | YRPQ9VRG_202609041025 | was_already_in_temp |
| 152 | #8U208GVY | LRY09Q9V_202609041027 | was_already_in_temp |
| 153 | #8VCGJ2VP | 2G99U98RQ_202609041023 | was_already_in_temp |
| 154 | #8YJU29PY | 8CLRLJC0_202609041021 | was_already_in_temp |
| 155 | #90YV8JYL | RUGC8UPR_202609041024 | was_already_in_temp |
| 156 | #92GG2JUP | 9Q000CQR_202609041028 | was_already_in_temp |
| 157 | #92VR9GYP | 2G8Q0V8JJ_202609041024 | was_already_in_temp |
| 158 | #9CV9R9GL | JR9YR888_202609041024 | was_already_in_temp |
| 159 | #9Q000CQR | 92GG2JUP_202609041028 | was_already_in_temp |
| 160 | #9Q0CJCVQ | 2R0GQ0Q29_202609041021 | was_already_in_temp |
| 161 | #9Q2V9QJJ | G2QLYV_202609041020 | was_already_in_temp |
| 162 | #CQVRQY0J | YP288JGJ_202609041021 | was_already_in_temp |
| 163 | #G29QQ8G8 | 2QPJR90LV_202609041021 | was_already_in_temp |
| 164 | #G2QLYV | 9Q2V9QJJ_202609041020 | was_already_in_temp |
| 165 | #G9PC2L00 | 2J9UG220G_202609041021 | was_already_in_temp |
| 166 | #GP9PYVL | 2YYPVL0G_202609041028 | was_already_in_temp |
| 167 | #GQ8YL09G | 2RLP9L8GU_202609041021 | was_already_in_temp |
| 168 | #GR8R0UG2 | 80U9U8P8_202609041025 | was_already_in_temp |
| 169 | #GRGVPLUV | 2U0PLVQJR_202609041021 | was_already_in_temp |
| 170 | #J0QLRVLU | 2G8PJCPJV_202609041024 | was_already_in_temp |
| 171 | #JL2U922G | RP9PJRVV_202609041028 | was_already_in_temp |
| 172 | #JQCV0CU | RRY9JUYC_202609041024 | was_already_in_temp |
| 173 | #JR9YR888 | 9CV9R9GL_202609041024 | was_already_in_temp |
| 174 | #JRPL08Q | 2CLCPR9J8_202609041020 | was_already_in_temp |
| 175 | #LJVYUQ0G | R9Q28YR8_202609041021 | was_already_in_temp |
| 176 | #LQPU2YUL | 20G8Q8VJ9_202609041027 | was_already_in_temp |
| 177 | #LQUL2200 | 2GVLRY90V_202609041026 | was_already_in_temp |
| 178 | #LRY09Q9V | 8U208GVY_202609041027 | was_already_in_temp |
| 179 | #LRYCCL9U | 2C2GRCLPL_202609041023 | was_already_in_temp |
| 180 | #LUV8LYR0 | PRCPUQJ_202609041028 | was_already_in_temp |
| 181 | #LY0Q88J0 | 2JJCPYJU8_202609041020 | was_already_in_temp |
| 182 | #LYJG8Q2 | U2U0Q92P_202609041025 | was_already_in_temp |
| 183 | #P2GYJ908 | 2L9JLG0VQ_202609041022 | was_already_in_temp |
| 184 | #P8QQC8U2 | RGPVYVV0_202609041021 | was_already_in_temp |
| 185 | #PG29VCL0 | 2J89Y28L0_202609041025 | was_already_in_temp |
| 186 | #PQGRGRY0 | 2LL8YVRRY_202609041024 | was_already_in_temp |
| 187 | #PRCPUQJ | LUV8LYR0_202609041028 | was_already_in_temp |
| 188 | #QUL9VGUY | 2GP0Y8Q89_202609041026 | was_already_in_temp |
| 189 | #R0C8QQGU | RYC828LY_202609041028 | was_already_in_temp |
| 190 | #R28PLQG | 2C82Q2JRR_202609041025 | was_already_in_temp |
| 191 | #R2LPV2VP | 2990CPLJ_202609041021 | was_already_in_temp |
| 192 | #R9Q28YR8 | LJVYUQ0G_202609041021 | was_already_in_temp |
| 193 | #RGPVYVV0 | P8QQC8U2_202609041021 | was_already_in_temp |
| 194 | #RGY9QYQC | 2G082PL0P_202609041021 | was_already_in_temp |
| 195 | #RP9PJRVV | JL2U922G_202609041028 | was_already_in_temp |
| 196 | #RQ9C2QU8 | 22YJVJRC9_202609041024 | was_already_in_temp |
| 197 | #RRY9JUYC | JQCV0CU_202609041024 | was_already_in_temp |
| 198 | #RUGC8UPR | 90YV8JYL_202609041024 | was_already_in_temp |
| 199 | #RYC828LY | R0C8QQGU_202609041028 | was_already_in_temp |
| 200 | #U2U0Q92P | LYJG8Q2_202609041025 | was_already_in_temp |
| 201 | #UY0RU2 | 20RP0RJYQ_202609041024 | was_already_in_temp |
| 202 | #UYPUQ8PY | 2LQ0V0CL_202609041023 | was_already_in_temp |
| 203 | #V9PGJCL8 | 2PY82PJP_202609041020 | was_already_in_temp |
| 204 | #VCGPYLLR | 2Q2LJ8LLQ_202609041028 | was_already_in_temp |
| 205 | #VP0VP8LC | 2U022GQ2U_202609041025 | was_already_in_temp |
| 206 | #Y9VP0VR0 | 2R0RV988Q_202609041023 | was_already_in_temp |
| 207 | #YCJUP882 | 2JGGGYJQR_202609041022 | was_already_in_temp |
| 208 | #YLV8YJRP | 2G88R9LU8_202609041025 | was_already_in_temp |
| 209 | #YP288JGJ | CQVRQY0J_202609041021 | was_already_in_temp |
| 210 | #YRPQ9VRG | 8QVVG0Q2_202609041025 | was_already_in_temp |
