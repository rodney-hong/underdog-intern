# Backfill game list: October 2026 run (2025-26)

Recorded 2026-10-22, after the pull and before any backfilled line is joined to a box-score result.
This is the game list required by "Entry: Test A on backfilled historical lines (2026-10-22)" in
preregistration.md. No result has been joined.

## Code

- Matching code: `match_event`, `_nickname` and `_et_date` in `backend/backfill.py` are identical to commit
  `a34f54e4256fe1a7ac9a1f47a02e45971d8b4b72`. Rule: a Kaggle game matches the one OddsAPI event whose
  commence_time, converted to ET, falls on the game's ET date and whose home and away nicknames equal the game's.
- The pull ran with that commit plus the per-(game, market) resume change, which is committed together with this file.

## Run

- Season 2025-26, newest first. Excluded: probe game 22500580.
- Markets (all games): `player_points,player_points_alternate,player_rebounds,player_rebounds_alternate,player_assists,player_assists_alternate,player_threes,player_threes_alternate`.
- Bookmakers: the live logger's 10 (underdog, fanduel, draftkings, prizepicks, novig, prophetx, kalshi, betmgm,
  espnbet, betrivers); includeMultipliers=true; one snapshot at the event's commence_time minus 30 minutes.
- Four chunks of --max-credits 4000 on 2026-10-22 between 16:40 and 16:44 ET; the fourth stopped at the floor
  (remaining 4,944 against a floor of 4,892).
- Games: 183 (182 ok, 1 empty), ET dates 2026-04-01 to 2026-06-13. 0 unmatched.
- Credits: 14,560 for event odds + 58 for 58 historical events calls.
- Empty: 42500222 (Lakers @ Thunder, Playoffs, 2026-05-07). OddsAPI returned no bookmakers at the T-30 snapshot
  (2026-05-08T01:05:38Z, event commence 2026-05-08T01:40:00Z) and billed 0 credits. It is not retried.

## Games pulled

| Kaggle gameId | ET date | game | type | OddsAPI event | snapshot requested | snapshot returned | status |
|---|---|---|---|---|---|---|---|
| 42500405 | 2026-06-13 | Knicks @ Spurs | Playoffs | 6cc5c3b9cfcb1d94bed9f3ca972b3114 | 2026-06-14T00:10:00Z | 2026-06-14T00:05:36Z | ok |
| 42500404 | 2026-06-10 | Spurs @ Knicks | Playoffs | 9ea306ebbe68ff94dfa5b88d1e250b37 | 2026-06-11T00:10:00Z | 2026-06-11T00:05:36Z | ok |
| 42500403 | 2026-06-08 | Spurs @ Knicks | Playoffs | 0707c5b20ff523e730c85090c7faffdf | 2026-06-09T00:10:00Z | 2026-06-09T00:05:36Z | ok |
| 42500402 | 2026-06-05 | Knicks @ Spurs | Playoffs | 2852e944f8ef0bc3eba79f883b9101ba | 2026-06-06T00:10:00Z | 2026-06-06T00:05:37Z | ok |
| 42500401 | 2026-06-03 | Knicks @ Spurs | Playoffs | 1aae688472781f1a1aaf3efdb38e884b | 2026-06-04T00:00:00Z | 2026-06-03T23:55:36Z | ok |
| 42500317 | 2026-05-30 | Spurs @ Thunder | Playoffs | c2c2561efd4d4f69367aa83be60995a0 | 2026-05-30T23:40:00Z | 2026-05-30T23:35:36Z | ok |
| 42500316 | 2026-05-28 | Thunder @ Spurs | Playoffs | 78fd894a8789f5fd7f2318cc85b2fc44 | 2026-05-29T00:10:00Z | 2026-05-29T00:05:37Z | ok |
| 42500315 | 2026-05-26 | Spurs @ Thunder | Playoffs | 197dd95ba7880a2cd6a00df88014610b | 2026-05-27T00:10:00Z | 2026-05-27T00:05:37Z | ok |
| 42500304 | 2026-05-25 | Knicks @ Cavaliers | Playoffs | a752ca36635e2ccb1bb5a9f387282e1c | 2026-05-25T23:40:00Z | 2026-05-25T23:35:37Z | ok |
| 42500314 | 2026-05-24 | Thunder @ Spurs | Playoffs | 74cd3b630383a2e3eff3afd92006ebb3 | 2026-05-24T23:40:00Z | 2026-05-24T23:35:37Z | ok |
| 42500303 | 2026-05-23 | Knicks @ Cavaliers | Playoffs | 4247d31ac7706ebe455b8f221c9e4184 | 2026-05-23T23:40:00Z | 2026-05-23T23:35:37Z | ok |
| 42500313 | 2026-05-22 | Thunder @ Spurs | Playoffs | 7b814a0bc57622abcbc708d9e9a27fca | 2026-05-23T00:10:00Z | 2026-05-23T00:05:37Z | ok |
| 42500302 | 2026-05-21 | Cavaliers @ Knicks | Playoffs | dc45982e3723fb38d8477a1a75395594 | 2026-05-21T23:40:00Z | 2026-05-21T23:35:37Z | ok |
| 42500312 | 2026-05-20 | Spurs @ Thunder | Playoffs | 51176769d0f89d3e39adc8e90922f84d | 2026-05-21T00:10:00Z | 2026-05-21T00:05:37Z | ok |
| 42500301 | 2026-05-19 | Cavaliers @ Knicks | Playoffs | 7e2d40a6d3b163e318b7abbe026c40c1 | 2026-05-19T23:40:00Z | 2026-05-19T23:35:37Z | ok |
| 42500311 | 2026-05-18 | Spurs @ Thunder | Playoffs | 924fe1c270c7ccb9e7a9cfa3002bdf05 | 2026-05-19T00:10:00Z | 2026-05-19T00:05:38Z | ok |
| 42500207 | 2026-05-17 | Cavaliers @ Pistons | Playoffs | 465746f51ad349641512f36c52306968 | 2026-05-17T23:40:00Z | 2026-05-17T23:35:37Z | ok |
| 42500236 | 2026-05-15 | Spurs @ Timberwolves | Playoffs | 1bd4432ce20bc61756180bf2d1c59c91 | 2026-05-16T01:10:00Z | 2026-05-16T01:05:38Z | ok |
| 42500206 | 2026-05-15 | Pistons @ Cavaliers | Playoffs | 745d265e0d83dac45060b44665d8cbe5 | 2026-05-15T22:40:00Z | 2026-05-15T22:35:38Z | ok |
| 42500205 | 2026-05-13 | Cavaliers @ Pistons | Playoffs | c3531530e77a4ada542bdbff23de5409 | 2026-05-13T23:40:00Z | 2026-05-13T23:35:37Z | ok |
| 42500235 | 2026-05-12 | Timberwolves @ Spurs | Playoffs | 09800f9101b348d4a66742a3d8888b82 | 2026-05-12T23:40:00Z | 2026-05-12T23:35:37Z | ok |
| 42500224 | 2026-05-11 | Thunder @ Lakers | Playoffs | 0db68bfa022e22873f48b8539addd5ae | 2026-05-12T02:10:00Z | 2026-05-12T02:05:37Z | ok |
| 42500204 | 2026-05-11 | Pistons @ Cavaliers | Playoffs | b537062ee0483d20ae932052628b663d | 2026-05-11T23:30:00Z | 2026-05-11T23:25:38Z | ok |
| 42500234 | 2026-05-10 | Spurs @ Timberwolves | Playoffs | 77e60e525f42e8337027f550bcd2f1cc | 2026-05-10T23:10:00Z | 2026-05-10T23:05:37Z | ok |
| 42500214 | 2026-05-10 | Knicks @ 76ers | Playoffs | 878243c543236baba132ad43ae333aae | 2026-05-10T19:10:00Z | 2026-05-10T19:05:37Z | ok |
| 42500223 | 2026-05-09 | Thunder @ Lakers | Playoffs | 40c8a29efb894c4dd6bf6acc5025c9cb | 2026-05-10T00:10:00Z | 2026-05-10T00:05:37Z | ok |
| 42500203 | 2026-05-09 | Pistons @ Cavaliers | Playoffs | 2734c350b90f332dbece882dd10b204e | 2026-05-09T18:40:00Z | 2026-05-09T18:35:37Z | ok |
| 42500233 | 2026-05-08 | Spurs @ Timberwolves | Playoffs | 1b5c084f510006e8f30cd6cac40e231d | 2026-05-09T01:10:00Z | 2026-05-09T01:05:37Z | ok |
| 42500213 | 2026-05-08 | Knicks @ 76ers | Playoffs | 30d614c5d781b768faf9cf885e890851 | 2026-05-08T22:40:00Z | 2026-05-08T22:35:37Z | ok |
| 42500222 | 2026-05-07 | Lakers @ Thunder | Playoffs | a306b74e13d87d2f8d64b1e01c2cf958 | 2026-05-08T01:10:00Z | 2026-05-08T01:05:38Z | empty |
| 42500202 | 2026-05-07 | Cavaliers @ Pistons | Playoffs | 862af0717b53a01b5e93a188a655d663 | 2026-05-07T22:40:00Z | 2026-05-07T22:35:38Z | ok |
| 42500232 | 2026-05-06 | Timberwolves @ Spurs | Playoffs | 246001ab42d44857430f7fe63f423461 | 2026-05-07T01:10:00Z | 2026-05-07T01:05:38Z | ok |
| 42500212 | 2026-05-06 | 76ers @ Knicks | Playoffs | d146e10295545e18ac8915a1f5860317 | 2026-05-06T22:40:00Z | 2026-05-06T22:35:38Z | ok |
| 42500221 | 2026-05-05 | Lakers @ Thunder | Playoffs | 689b4554b0b10e67f46872eb2e74b98d | 2026-05-06T00:10:00Z | 2026-05-06T00:05:38Z | ok |
| 42500201 | 2026-05-05 | Cavaliers @ Pistons | Playoffs | 0d7933fbc3dd3e384f9b709cff4e882c | 2026-05-05T22:40:00Z | 2026-05-05T22:35:38Z | ok |
| 42500231 | 2026-05-04 | Timberwolves @ Spurs | Playoffs | 219920e3a715ceb3dc69f3638f7571d5 | 2026-05-05T01:10:00Z | 2026-05-05T01:05:37Z | ok |
| 42500211 | 2026-05-04 | 76ers @ Knicks | Playoffs | b11391b35b7a0cf79deeef3bb3eb9009 | 2026-05-04T23:40:00Z | 2026-05-04T23:35:37Z | ok |
| 42500137 | 2026-05-03 | Raptors @ Cavaliers | Playoffs | a463f640631f8b0580966b909965de14 | 2026-05-03T23:10:00Z | 2026-05-03T23:05:37Z | ok |
| 42500107 | 2026-05-03 | Magic @ Pistons | Playoffs | be60e6f50fc77fc3a3cf6925472bad91 | 2026-05-03T19:10:00Z | 2026-05-03T19:05:37Z | ok |
| 42500117 | 2026-05-02 | 76ers @ Celtics | Playoffs | a37e80a2e3cf3aaa3ae84a9124987058 | 2026-05-02T23:10:00Z | 2026-05-02T23:05:37Z | ok |
| 42500176 | 2026-05-01 | Lakers @ Rockets | Playoffs | c361a0b8c035a055bd9497b52674e3fb | 2026-05-02T01:10:00Z | 2026-05-02T01:05:38Z | ok |
| 42500136 | 2026-05-01 | Cavaliers @ Raptors | Playoffs | d1938a9c13602a661e894f996f2680e8 | 2026-05-01T23:10:00Z | 2026-05-01T23:05:37Z | ok |
| 42500106 | 2026-05-01 | Pistons @ Magic | Playoffs | 88e10294c54deffd6e9e6efcba249d2c | 2026-05-01T22:40:00Z | 2026-05-01T22:35:38Z | ok |
| 42500166 | 2026-04-30 | Nuggets @ Timberwolves | Playoffs | b981ac0f551b6f109cd18fa31de439b4 | 2026-05-01T01:10:00Z | 2026-05-01T01:05:38Z | ok |
| 42500126 | 2026-04-30 | Knicks @ Hawks | Playoffs | 24992ea72a9115eaa601ff602cc60468 | 2026-04-30T22:40:00Z | 2026-04-30T22:35:38Z | ok |
| 42500116 | 2026-04-30 | Celtics @ 76ers | Playoffs | b2f1e46d7adfcf49fa68a52df4938626 | 2026-04-30T23:40:00Z | 2026-04-30T23:35:39Z | ok |
| 42500175 | 2026-04-29 | Rockets @ Lakers | Playoffs | 0d0aa62d4305ee094389473b308ce0bd | 2026-04-30T01:40:00Z | 2026-04-30T01:35:38Z | ok |
| 42500135 | 2026-04-29 | Raptors @ Cavaliers | Playoffs | 6fa2ebdc614d327bc9a4558541adcd30 | 2026-04-29T23:10:00Z | 2026-04-29T23:05:38Z | ok |
| 42500105 | 2026-04-29 | Magic @ Pistons | Playoffs | 912bee9cc9d36b6017819f012c0c56f1 | 2026-04-29T22:40:00Z | 2026-04-29T22:35:38Z | ok |
| 42500155 | 2026-04-28 | Trail Blazers @ Spurs | Playoffs | f61ea629819d5176316c94a379c02ad2 | 2026-04-29T01:10:00Z | 2026-04-29T01:05:38Z | ok |
| 42500125 | 2026-04-28 | Hawks @ Knicks | Playoffs | 595b24bea54d0917338e4a51372398b3 | 2026-04-28T23:40:00Z | 2026-04-28T23:35:37Z | ok |
| 42500115 | 2026-04-28 | 76ers @ Celtics | Playoffs | d749c50ad2a2660df91f9dc1b8e20d78 | 2026-04-28T22:40:00Z | 2026-04-28T22:35:38Z | ok |
| 42500165 | 2026-04-27 | Timberwolves @ Nuggets | Playoffs | e4d639b6c49e5fd0accc6e80636e88fe | 2026-04-28T02:10:00Z | 2026-04-28T02:05:39Z | ok |
| 42500144 | 2026-04-27 | Thunder @ Suns | Playoffs | b3eb8f644d05290981f8805f5410f24a | 2026-04-28T01:10:00Z | 2026-04-28T01:05:38Z | ok |
| 42500104 | 2026-04-27 | Pistons @ Magic | Playoffs | 846b0e9b691b5a291619fa22f96c7dfa | 2026-04-27T23:40:00Z | 2026-04-27T23:35:37Z | ok |
| 42500174 | 2026-04-26 | Lakers @ Rockets | Playoffs | 399d2e69e4408801d09e614c88779989 | 2026-04-27T01:10:00Z | 2026-04-27T01:05:37Z | ok |
| 42500154 | 2026-04-26 | Spurs @ Trail Blazers | Playoffs | 0cab123e90bdf5d1d144a7e26ebc339c | 2026-04-26T19:10:00Z | 2026-04-26T19:05:37Z | ok |
| 42500134 | 2026-04-26 | Cavaliers @ Raptors | Playoffs | d94d967f8600a3fb8b869f32c31a7357 | 2026-04-26T16:40:00Z | 2026-04-26T16:35:37Z | ok |
| 42500114 | 2026-04-26 | Celtics @ 76ers | Playoffs | 62dc6619019d4366984883a71b283077 | 2026-04-26T22:40:00Z | 2026-04-26T22:35:37Z | ok |
| 42500164 | 2026-04-25 | Nuggets @ Timberwolves | Playoffs | b24e970fd53cf667dd009e54d107d4b1 | 2026-04-26T00:10:00Z | 2026-04-26T00:05:37Z | ok |
| 42500143 | 2026-04-25 | Thunder @ Suns | Playoffs | 9f41c322eb6aa09683dc241cacc31d7c | 2026-04-25T19:10:00Z | 2026-04-25T19:05:37Z | ok |
| 42500124 | 2026-04-25 | Knicks @ Hawks | Playoffs | 184d31a9a32dffc9c91eac3766d6f12d | 2026-04-25T21:40:00Z | 2026-04-25T21:35:37Z | ok |
| 42500103 | 2026-04-25 | Pistons @ Magic | Playoffs | e480da9a1b9dacbed9120388db4f133f | 2026-04-25T16:40:00Z | 2026-04-25T16:35:37Z | ok |
| 42500173 | 2026-04-24 | Lakers @ Rockets | Playoffs | 5000a9f1dd7ba71de7ae6ceef836aa48 | 2026-04-24T23:40:00Z | 2026-04-24T23:35:38Z | ok |
| 42500153 | 2026-04-24 | Spurs @ Trail Blazers | Playoffs | af4847d63733e67ddd02f2bafac8ea6f | 2026-04-25T02:10:00Z | 2026-04-25T02:05:38Z | ok |
| 42500113 | 2026-04-24 | Celtics @ 76ers | Playoffs | 8d8ab21ae69d529ee4bd01fb8ace8276 | 2026-04-24T22:40:00Z | 2026-04-24T22:35:38Z | ok |
| 42500163 | 2026-04-23 | Nuggets @ Timberwolves | Playoffs | c548073cbcde990c77986a123bd2d5bd | 2026-04-24T01:00:00Z | 2026-04-24T00:55:38Z | ok |
| 42500133 | 2026-04-23 | Cavaliers @ Raptors | Playoffs | b754568efb2036d7f3b661cc8093e66f | 2026-04-23T23:40:00Z | 2026-04-23T23:35:38Z | ok |
| 42500123 | 2026-04-23 | Knicks @ Hawks | Playoffs | b2b129565e14b8782b318b699b05ab3b | 2026-04-23T22:40:00Z | 2026-04-23T22:35:38Z | ok |
| 42500142 | 2026-04-22 | Suns @ Thunder | Playoffs | 0ce48ecb61f40bc3bd784adfe7f8e811 | 2026-04-23T01:10:00Z | 2026-04-23T01:05:38Z | ok |
| 42500102 | 2026-04-22 | Magic @ Pistons | Playoffs | c9bc6bcae048283407d1cb53995eee8e | 2026-04-22T22:40:00Z | 2026-04-22T22:35:38Z | ok |
| 42500172 | 2026-04-21 | Rockets @ Lakers | Playoffs | d3d28b764e5099920880de59a45e82cf | 2026-04-22T02:10:00Z | 2026-04-22T02:05:38Z | ok |
| 42500152 | 2026-04-21 | Trail Blazers @ Spurs | Playoffs | 680084e4612c197a1cf4170d82ba6f10 | 2026-04-21T23:30:00Z | 2026-04-21T23:25:38Z | ok |
| 42500112 | 2026-04-21 | 76ers @ Celtics | Playoffs | d1a7a6e7b024f888e922a62b14fcf466 | 2026-04-21T22:40:00Z | 2026-04-21T22:35:38Z | ok |
| 42500162 | 2026-04-20 | Timberwolves @ Nuggets | Playoffs | a52365fe7f7123df1c4220d9591a6da2 | 2026-04-21T02:10:00Z | 2026-04-21T02:05:38Z | ok |
| 42500132 | 2026-04-20 | Raptors @ Cavaliers | Playoffs | 273905d6a4ae9217ec0734b66a0eb28b | 2026-04-20T22:40:00Z | 2026-04-20T22:35:38Z | ok |
| 42500122 | 2026-04-20 | Hawks @ Knicks | Playoffs | a1b444bd00a27cbae3cf7ca3bf75a3f0 | 2026-04-20T23:40:00Z | 2026-04-20T23:35:38Z | ok |
| 42500151 | 2026-04-19 | Trail Blazers @ Spurs | Playoffs | 04e3ad1ccbad6419ac711ff120fd5cbe | 2026-04-20T00:40:00Z | 2026-04-20T00:35:37Z | ok |
| 42500141 | 2026-04-19 | Suns @ Thunder | Playoffs | ef6021edff80a85fe2dae5cf3530d9d4 | 2026-04-19T19:10:00Z | 2026-04-19T19:05:37Z | ok |
| 42500111 | 2026-04-19 | 76ers @ Celtics | Playoffs | 00d1efd43d0790297fb68684a9171191 | 2026-04-19T16:40:00Z | 2026-04-19T16:35:37Z | ok |
| 42500101 | 2026-04-19 | Magic @ Pistons | Playoffs | e777e2f049a5c85aed96a220a2cfe924 | 2026-04-19T22:10:00Z | 2026-04-19T22:05:37Z | ok |
| 42500171 | 2026-04-18 | Rockets @ Lakers | Playoffs | 1a9383105929a09e5662227ad45554b8 | 2026-04-19T00:10:00Z | 2026-04-19T00:05:38Z | ok |
| 42500161 | 2026-04-18 | Timberwolves @ Nuggets | Playoffs | 526f783434e5dabca25ee032d8fee612 | 2026-04-18T19:10:00Z | 2026-04-18T19:05:37Z | ok |
| 42500131 | 2026-04-18 | Raptors @ Cavaliers | Playoffs | 2a8e5a45f1bcb477a6f9cbd7739d005e | 2026-04-18T16:40:00Z | 2026-04-18T16:35:38Z | ok |
| 42500121 | 2026-04-18 | Hawks @ Knicks | Playoffs | bee2c9ea4bf8df373aa45af7005b583d | 2026-04-18T21:40:00Z | 2026-04-18T21:35:37Z | ok |
| 52500211 | 2026-04-17 | Warriors @ Suns | Play-in Tournament | c8d1b7fd06c21e259b9416daf596f023 | 2026-04-18T01:40:00Z | 2026-04-18T01:35:38Z | ok |
| 52500201 | 2026-04-17 | Hornets @ Magic | Play-in Tournament | fc16dfbe5b438857b93902fa811dd8f6 | 2026-04-17T23:10:00Z | 2026-04-17T23:05:38Z | ok |
| 52500131 | 2026-04-15 | Warriors @ Clippers | Play-in Tournament | 7cfae0891b99865ef7fc9d357b69ca28 | 2026-04-16T01:40:00Z | 2026-04-16T01:35:37Z | ok |
| 52500101 | 2026-04-15 | Magic @ 76ers | Play-in Tournament | f7264c00c662a127d58f4ab12fc498bc | 2026-04-15T23:10:00Z | 2026-04-15T23:05:38Z | ok |
| 52500121 | 2026-04-14 | Trail Blazers @ Suns | Play-in Tournament | 0999cb23cd03811476b378dd4045ea16 | 2026-04-15T01:40:00Z | 2026-04-15T01:35:38Z | ok |
| 52500111 | 2026-04-14 | Heat @ Hornets | Play-in Tournament | 25f53495c4044a1d8fb682e3715eb7b3 | 2026-04-14T23:10:00Z | 2026-04-14T23:05:38Z | ok |
| 22501200 | 2026-04-12 | Kings @ Trail Blazers | Regular Season | 3f5adb3e93b0c63eb14ed1d0e8b90a43 | 2026-04-13T00:10:00Z | 2026-04-13T00:05:37Z | ok |
| 22501199 | 2026-04-12 | Warriors @ Clippers | Regular Season | 06ccc7d0ba2acaf031c7d42eea09672c | 2026-04-13T00:10:00Z | 2026-04-13T00:05:37Z | ok |
| 22501198 | 2026-04-12 | Jazz @ Lakers | Regular Season | 3f3bec3a69985062b105b5fe8bf06fe9 | 2026-04-13T00:10:00Z | 2026-04-13T00:05:37Z | ok |
| 22501197 | 2026-04-12 | Nuggets @ Spurs | Regular Season | 09604d4c311c1eb2215317d12154f42a | 2026-04-13T00:10:00Z | 2026-04-13T00:05:37Z | ok |
| 22501196 | 2026-04-12 | Suns @ Thunder | Regular Season | 8c4fafe262a5424f243a130d7ab60e82 | 2026-04-13T00:10:00Z | 2026-04-13T00:05:37Z | ok |
| 22501195 | 2026-04-12 | Pelicans @ Timberwolves | Regular Season | 47ede7b5685ae38218b5c2ea1ffd1699 | 2026-04-13T00:10:00Z | 2026-04-13T00:05:37Z | ok |
| 22501194 | 2026-04-12 | Grizzlies @ Rockets | Regular Season | fdd561cce076bab5eb0f0651d1d0f8e4 | 2026-04-13T00:10:00Z | 2026-04-13T00:05:37Z | ok |
| 22501193 | 2026-04-12 | Bulls @ Mavericks | Regular Season | c8cde1aa908581185caf0bb87659055a | 2026-04-13T00:10:00Z | 2026-04-13T00:05:37Z | ok |
| 22501192 | 2026-04-12 | Nets @ Raptors | Regular Season | d55bfef33abc5dc6dba0717236d0ee80 | 2026-04-12T21:40:00Z | 2026-04-12T21:35:37Z | ok |
| 22501191 | 2026-04-12 | Bucks @ 76ers | Regular Season | 23c66e71d25675800d939e532e38bcd2 | 2026-04-12T21:40:00Z | 2026-04-12T21:35:37Z | ok |
| 22501190 | 2026-04-12 | Hornets @ Knicks | Regular Season | 38b10a0526d5b246097a502269bcdf71 | 2026-04-12T21:40:00Z | 2026-04-12T21:35:37Z | ok |
| 22501189 | 2026-04-12 | Hawks @ Heat | Regular Season | 22dcb3e32847c7eab5774e8927016216 | 2026-04-12T21:40:00Z | 2026-04-12T21:35:37Z | ok |
| 22501188 | 2026-04-12 | Pistons @ Pacers | Regular Season | 4675acfb68965858aa571a0493a222da | 2026-04-12T21:40:00Z | 2026-04-12T21:35:37Z | ok |
| 22501187 | 2026-04-12 | Wizards @ Cavaliers | Regular Season | ad75b36adad0af42dea87b9bcdfa91f8 | 2026-04-12T21:40:00Z | 2026-04-12T21:35:37Z | ok |
| 22501186 | 2026-04-12 | Magic @ Celtics | Regular Season | f78d40af453b24c39e18559579894d14 | 2026-04-12T21:40:00Z | 2026-04-12T21:35:37Z | ok |
| 22501185 | 2026-04-10 | Suns @ Lakers | Regular Season | 1935906088ec1cf3d2f989ace06a8acb | 2026-04-11T02:10:00Z | 2026-04-11T02:05:38Z | ok |
| 22501184 | 2026-04-10 | Warriors @ Kings | Regular Season | d6099dffbcac5bbcdf69fd35c6229ff7 | 2026-04-11T01:40:00Z | 2026-04-11T01:35:39Z | ok |
| 22501183 | 2026-04-10 | Clippers @ Trail Blazers | Regular Season | 858993998045b68682b2aae461cb4037 | 2026-04-11T01:40:00Z | 2026-04-11T01:35:39Z | ok |
| 22501182 | 2026-04-10 | Thunder @ Nuggets | Regular Season | 11fa8c6f7710a287909a9765aa925cce | 2026-04-11T00:40:00Z | 2026-04-11T00:35:38Z | ok |
| 22501181 | 2026-04-10 | Grizzlies @ Jazz | Regular Season | 88bf78be502ce70dd7e5e669cd9f95df | 2026-04-11T01:10:00Z | 2026-04-11T01:05:37Z | ok |
| 22501180 | 2026-04-10 | Mavericks @ Spurs | Regular Season | 51aa38f3acd638a314975c61ab16a862 | 2026-04-10T23:40:00Z | 2026-04-10T23:35:37Z | ok |
| 22501179 | 2026-04-10 | Nets @ Bucks | Regular Season | d463e0aa51436ff32f8ee83d6d3893a8 | 2026-04-10T23:40:00Z | 2026-04-10T23:35:37Z | ok |
| 22501178 | 2026-04-10 | Timberwolves @ Rockets | Regular Season | 1fbdc557471472d90b606f5e50853915 | 2026-04-11T01:10:00Z | 2026-04-11T01:05:37Z | ok |
| 22501177 | 2026-04-10 | Magic @ Bulls | Regular Season | ebbf0d3885094991ff36055e753635a3 | 2026-04-10T23:40:00Z | 2026-04-10T23:35:37Z | ok |
| 22501176 | 2026-04-10 | Raptors @ Knicks | Regular Season | 39083e08e7959b0ec516933d574379cd | 2026-04-10T23:10:00Z | 2026-04-10T23:05:38Z | ok |
| 22501175 | 2026-04-10 | 76ers @ Pacers | Regular Season | 4398234154ac742bd22d689c8d4bf956 | 2026-04-10T23:10:00Z | 2026-04-10T23:05:38Z | ok |
| 22501174 | 2026-04-10 | Pelicans @ Celtics | Regular Season | 80adae4b4459f5602f7500d94892f2a5 | 2026-04-10T23:10:00Z | 2026-04-10T23:05:38Z | ok |
| 22501173 | 2026-04-10 | Cavaliers @ Hawks | Regular Season | 70a2274c4f82c7a3f37879b7edf0cfa4 | 2026-04-10T22:40:00Z | 2026-04-10T22:35:38Z | ok |
| 22501172 | 2026-04-10 | Heat @ Wizards | Regular Season | f47d78a747510d6ef00500f74872372d | 2026-04-10T22:40:00Z | 2026-04-10T22:35:38Z | ok |
| 22501171 | 2026-04-10 | Pistons @ Hornets | Regular Season | 1126b937945abff19e12dc1ac60f2ecb | 2026-04-10T22:40:00Z | 2026-04-10T22:35:38Z | ok |
| 22501170 | 2026-04-09 | Lakers @ Warriors | Regular Season | 29fe17f5b19da82ce2957c3683a10aa7 | 2026-04-10T01:40:00Z | 2026-04-10T01:35:38Z | ok |
| 22501169 | 2026-04-09 | 76ers @ Rockets | Regular Season | 30b372ff1973370201b644c207be2dd3 | 2026-04-09T23:40:00Z | 2026-04-09T23:35:38Z | ok |
| 22501168 | 2026-04-09 | Celtics @ Knicks | Regular Season | bf30a732402325cab54ab666ee0f5c11 | 2026-04-09T23:10:00Z | 2026-04-09T23:05:38Z | ok |
| 22501167 | 2026-04-09 | Pacers @ Nets | Regular Season | 625bbdf7bc0bdf1d57b3f97742908221 | 2026-04-09T23:10:00Z | 2026-04-09T23:05:38Z | ok |
| 22501166 | 2026-04-09 | Bulls @ Wizards | Regular Season | 0852cffec341701f6a0dde8619ae9f56 | 2026-04-09T22:40:00Z | 2026-04-09T22:35:38Z | ok |
| 22501165 | 2026-04-09 | Heat @ Raptors | Regular Season | 53b49bbe56f8581f353bbbb5c6179ac5 | 2026-04-09T22:40:00Z | 2026-04-09T22:35:38Z | ok |
| 22501164 | 2026-04-08 | Mavericks @ Suns | Regular Season | 3e95057b94b2d930c8cc201db634eaf8 | 2026-04-09T01:40:00Z | 2026-04-09T01:35:38Z | ok |
| 22501163 | 2026-04-08 | Thunder @ Clippers | Regular Season | 68ab4a35de88dba88af31eeb3ba710fb | 2026-04-09T01:40:00Z | 2026-04-09T01:35:38Z | ok |
| 22501162 | 2026-04-08 | Grizzlies @ Nuggets | Regular Season | 41c07b1937b8a59703253dc470ca4ddb | 2026-04-09T00:40:00Z | 2026-04-09T00:35:38Z | ok |
| 22501161 | 2026-04-08 | Trail Blazers @ Spurs | Regular Season | 08c9fd2b6b80f1f763e2c2eb3e935208 | 2026-04-08T23:40:00Z | 2026-04-08T23:35:38Z | ok |
| 22501160 | 2026-04-08 | Bucks @ Pistons | Regular Season | c14b4c18b6b1e91a53302fdcc32910f2 | 2026-04-08T22:40:00Z | 2026-04-08T22:35:37Z | ok |
| 22501159 | 2026-04-08 | Timberwolves @ Magic | Regular Season | cc5e682d9f44baa1c9d41eb3fd1c89b0 | 2026-04-08T22:40:00Z | 2026-04-08T22:35:37Z | ok |
| 22501158 | 2026-04-08 | Hawks @ Cavaliers | Regular Season | 3fe42fd427985fed1fee7c2e58935a67 | 2026-04-08T22:40:00Z | 2026-04-08T22:35:37Z | ok |
| 22501157 | 2026-04-07 | Rockets @ Suns | Regular Season | 6ec00d2267f0fe20067d4e2363a17162 | 2026-04-08T02:40:00Z | 2026-04-08T02:35:37Z | ok |
| 22501156 | 2026-04-07 | Mavericks @ Clippers | Regular Season | 3133467540e08b88d68625ed9e975399 | 2026-04-08T02:10:00Z | 2026-04-08T02:05:37Z | ok |
| 22501155 | 2026-04-07 | Thunder @ Lakers | Regular Season | 01f002eb30dfd24045740d4fbf68207e | 2026-04-08T02:10:00Z | 2026-04-08T02:05:37Z | ok |
| 22501154 | 2026-04-07 | Kings @ Warriors | Regular Season | 74efed6fa8060207aa1784a960c6141c | 2026-04-08T01:40:00Z | 2026-04-08T01:35:38Z | ok |
| 22501153 | 2026-04-07 | Jazz @ Pelicans | Regular Season | 24016853f9722af6d4829341d7ae4d81 | 2026-04-07T23:40:00Z | 2026-04-07T23:35:38Z | ok |
| 22501152 | 2026-04-07 | Timberwolves @ Pacers | Regular Season | be5171067867c61067c779345515c72d | 2026-04-07T22:40:00Z | 2026-04-07T22:35:38Z | ok |
| 22501151 | 2026-04-07 | Heat @ Raptors | Regular Season | 9aaa887e3f4a853902d6613072b2f35b | 2026-04-07T23:10:00Z | 2026-04-07T23:05:38Z | ok |
| 22501150 | 2026-04-07 | Bucks @ Nets | Regular Season | d669fae8ce6b2ba2ca97fb61dc09e2ce | 2026-04-07T23:10:00Z | 2026-04-07T23:05:38Z | ok |
| 22501149 | 2026-04-07 | Hornets @ Celtics | Regular Season | 96d26f7a098eba89636e1a99c7c79d8d | 2026-04-07T23:40:00Z | 2026-04-07T23:35:38Z | ok |
| 22501148 | 2026-04-07 | Bulls @ Wizards | Regular Season | 9f4ac4d421b28dcb2764f555eb8a8f0c | 2026-04-07T22:40:00Z | 2026-04-07T22:35:38Z | ok |
| 22501147 | 2026-04-06 | Trail Blazers @ Nuggets | Regular Season | 76e52ccb4140e4936de62dbadae0808d | 2026-04-07T00:40:00Z | 2026-04-07T00:35:37Z | ok |
| 22501146 | 2026-04-06 | 76ers @ Spurs | Regular Season | 14dede373d304abbddc590d2bcc81e4e | 2026-04-06T23:40:00Z | 2026-04-06T23:35:37Z | ok |
| 22501145 | 2026-04-06 | Cavaliers @ Grizzlies | Regular Season | c2ff7eb921314ca5ebc2ad18db189374 | 2026-04-06T23:40:00Z | 2026-04-06T23:35:37Z | ok |
| 22501144 | 2026-04-06 | Pistons @ Magic | Regular Season | ec27c42246b5e40c34f4c2d93930ed20 | 2026-04-06T22:40:00Z | 2026-04-06T22:35:37Z | ok |
| 22501143 | 2026-04-06 | Knicks @ Hawks | Regular Season | dce0f3f389617fa7daa990c209ee8acc | 2026-04-06T22:40:00Z | 2026-04-06T22:35:37Z | ok |
| 22501142 | 2026-04-05 | Rockets @ Warriors | Regular Season | 8516988340b00d09278e04abd65885ad | 2026-04-06T01:40:00Z | 2026-04-06T01:35:38Z | ok |
| 22501141 | 2026-04-05 | Clippers @ Kings | Regular Season | f01f7ab4597dc0c1a08e837d9b9beae9 | 2026-04-06T00:40:00Z | 2026-04-06T00:35:38Z | ok |
| 22501140 | 2026-04-05 | Lakers @ Mavericks | Regular Season | c6dfafffbada89cc908addd3a1f7748b | 2026-04-05T23:10:00Z | 2026-04-05T23:05:38Z | ok |
| 22501139 | 2026-04-05 | Jazz @ Thunder | Regular Season | 5441d9423f924c9a489ca00a7e321466 | 2026-04-05T22:40:00Z | 2026-04-05T22:35:37Z | ok |
| 22501138 | 2026-04-05 | Magic @ Pelicans | Regular Season | 9f54cd277a7d7a9fe0de35d568dfa4af | 2026-04-05T22:40:00Z | 2026-04-05T22:35:37Z | ok |
| 22501137 | 2026-04-05 | Hornets @ Timberwolves | Regular Season | 8bce3373da945128589ce3cabae25443 | 2026-04-05T22:40:00Z | 2026-04-05T22:35:37Z | ok |
| 22501136 | 2026-04-05 | Pacers @ Cavaliers | Regular Season | 4f3d29a0317850ac56b0c6db31223705 | 2026-04-05T21:40:00Z | 2026-04-05T21:35:38Z | ok |
| 22501135 | 2026-04-05 | Grizzlies @ Bucks | Regular Season | 5ce196844b4f956a3c6961eec5bc859b | 2026-04-05T19:10:00Z | 2026-04-05T19:05:37Z | ok |
| 22501134 | 2026-04-05 | Suns @ Bulls | Regular Season | 8071f80d106d512c88016b240e766d84 | 2026-04-05T19:10:00Z | 2026-04-05T19:05:37Z | ok |
| 22501133 | 2026-04-05 | Wizards @ Nets | Regular Season | c1b3d187f022b0870bfa7128e1d02ae5 | 2026-04-05T19:10:00Z | 2026-04-05T19:05:37Z | ok |
| 22501132 | 2026-04-05 | Raptors @ Celtics | Regular Season | 6f1339b3519de542ab74c59857bfeff1 | 2026-04-05T19:10:00Z | 2026-04-05T19:05:37Z | ok |
| 22501131 | 2026-04-04 | Pistons @ 76ers | Regular Season | dc7b446ad8c325394b7d604d3e17ad7b | 2026-04-04T22:40:00Z | 2026-04-04T22:35:37Z | ok |
| 22501130 | 2026-04-04 | Spurs @ Nuggets | Regular Season | 53273367f28e360f2e7814dd06e29a24 | 2026-04-04T18:40:00Z | 2026-04-04T18:35:38Z | ok |
| 22501129 | 2026-04-04 | Wizards @ Heat | Regular Season | b146607a8fe26fe467294dfdbaa0ff2b | 2026-04-04T18:40:00Z | 2026-04-04T18:35:38Z | ok |
| 22501128 | 2026-04-03 | Pelicans @ Kings | Regular Season | 2c0009c3a722545d107730244928ad22 | 2026-04-04T01:40:00Z | 2026-04-04T01:35:38Z | ok |
| 22501127 | 2026-04-03 | Magic @ Mavericks | Regular Season | 905392b9896b3f0d42e43bc4af97d72e | 2026-04-04T00:10:00Z | 2026-04-04T00:05:38Z | ok |
| 22501126 | 2026-04-03 | Celtics @ Bucks | Regular Season | d33be5a00d538b7bd7dee98ed5166593 | 2026-04-03T23:40:00Z | 2026-04-03T23:35:38Z | ok |
| 22501125 | 2026-04-03 | Raptors @ Grizzlies | Regular Season | 3880a34134efe663dcdc776fff7cde38 | 2026-04-03T23:40:00Z | 2026-04-03T23:35:38Z | ok |
| 22501124 | 2026-04-03 | Jazz @ Rockets | Regular Season | e9a2348d7311c62fe296a00ed8c03e29 | 2026-04-03T23:40:00Z | 2026-04-03T23:35:38Z | ok |
| 22501123 | 2026-04-03 | Bulls @ Knicks | Regular Season | d2565f4ec40ef0701e442d56c8d44ff2 | 2026-04-03T23:10:00Z | 2026-04-03T23:05:38Z | ok |
| 22501122 | 2026-04-03 | Hawks @ Nets | Regular Season | cb55f248d8dbb539ca55edf31d762f6a | 2026-04-03T23:10:00Z | 2026-04-03T23:05:38Z | ok |
| 22501121 | 2026-04-03 | Timberwolves @ 76ers | Regular Season | 21031d5ee035e890ee79d1afeef2f40c | 2026-04-03T22:40:00Z | 2026-04-03T22:35:38Z | ok |
| 22501120 | 2026-04-03 | Pacers @ Hornets | Regular Season | 033717528554996f232e2517ea1ad281 | 2026-04-03T22:40:00Z | 2026-04-03T22:35:38Z | ok |
| 22501119 | 2026-04-02 | Spurs @ Clippers | Regular Season | 8acdc53c485cd361a907edc8d53091f2 | 2026-04-03T02:10:00Z | 2026-04-03T02:05:38Z | ok |
| 22501118 | 2026-04-02 | Pelicans @ Trail Blazers | Regular Season | 52a9bdfa0493e80275ae71f49de1146f | 2026-04-03T01:40:00Z | 2026-04-03T01:35:38Z | ok |
| 22501117 | 2026-04-02 | Cavaliers @ Warriors | Regular Season | 67a88265216bd48208f38288dba434ba | 2026-04-03T01:40:00Z | 2026-04-03T01:35:38Z | ok |
| 22501116 | 2026-04-02 | Lakers @ Thunder | Regular Season | 97f990a7956a23f16f9298270f5b556e | 2026-04-03T01:10:00Z | 2026-04-03T01:05:38Z | ok |
| 22501115 | 2026-04-02 | Timberwolves @ Pistons | Regular Season | b70520c84d1db67fb83d5d3666ef1d46 | 2026-04-02T22:40:00Z | 2026-04-02T22:35:38Z | ok |
| 22501114 | 2026-04-02 | Suns @ Hornets | Regular Season | 289fb44a6171fe5e7365b05dca97889e | 2026-04-02T22:40:00Z | 2026-04-02T22:35:38Z | ok |
| 22501113 | 2026-04-01 | Spurs @ Warriors | Regular Season | 54540a1bafcd2e0cff9b5cd8a09e0bfa | 2026-04-02T01:30:00Z | 2026-04-02T01:25:38Z | ok |
| 22501112 | 2026-04-01 | Nuggets @ Jazz | Regular Season | 8b86324be55670d1f3c59008ae771dac | 2026-04-02T00:40:00Z | 2026-04-02T00:35:38Z | ok |
| 22501110 | 2026-04-01 | Bucks @ Rockets | Regular Season | 6f4627df908d38c70424ce6896309b37 | 2026-04-01T23:30:00Z | 2026-04-01T23:25:38Z | ok |
| 22501109 | 2026-04-01 | Pacers @ Bulls | Regular Season | 1e9f8048801efb165d39e008270e4942 | 2026-04-01T23:40:00Z | 2026-04-01T23:35:38Z | ok |
| 22501108 | 2026-04-01 | Kings @ Raptors | Regular Season | 333a6fd05a4f8492b95e9d9426450430 | 2026-04-01T23:30:00Z | 2026-04-01T23:25:38Z | ok |

## Probe games (not part of this run)

| Kaggle gameId | ET date | game | markets requested | status | note |
|---|---|---|---|---|---|
| 22500580 | 2026-01-15 | Celtics @ Heat | player_points,player_points_alternate,player_rebounds,player_rebounds_alternate,player_assists,player_assists_alternate,player_threes,player_threes_alternate,player_steals,player_blocks | ok | stage 3 probe; excluded from Test A by the pre-registration |
| 22300571 | 2024-01-17 | Spurs @ Celtics | player_points,player_points_alternate,player_rebounds,player_rebounds_alternate,player_assists,player_assists_alternate,player_threes,player_threes_alternate,player_steals,player_blocks | ok | stage 3 probe; excluded from Test A by the pre-registration |
| 22400562 | 2025-01-15 | Celtics @ Raptors | player_points | ok | 2024-25 Underdog-coverage probe (player_points only); not named in the pre-registration |
