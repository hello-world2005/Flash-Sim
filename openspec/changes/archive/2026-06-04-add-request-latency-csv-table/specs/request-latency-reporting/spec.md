## MODIFIED Requirements

### Requirement: Event-driven simulation exports one latency report entry per input request

褰撲簨浠堕┍鍔ㄤ豢鐪熷畬鎴愬悗锛岀郴缁?SHALL 鍦?`report/` 鐩綍涓嬪鍑轰竴涓姹傜骇 JSON 鎶ュ憡锛屽苟 MUST 鍚屾椂鐢熸垚涓€浠藉悓鍩哄悕鐨?CSV 鎶ュ憡銆傛姤鍛?MUST 涓鸿緭鍏?trace 涓殑姣忎竴鏉¤姹傜敓鎴愪竴鏉¤褰曪紝骞跺寘鍚ǔ瀹氱殑璇锋眰鏍囪瘑銆乼race 椤哄簭銆佽姹傜被鍨嬨€佽緭鍏ュ湴鍧€鑼冨洿銆佽姹傜姸鎬佸拰鎬绘椂寤跺瓧娈点€俉SV 蹇呴』淇濈暀涓庡悓涓€ trace JSON 鎶ュ憡涓€鑷寸殑璇锋眰琛屾暟銆?

#### Scenario: Successful simulation writes per-request JSON and CSV reports

- **WHEN** `Engine.Start_simulation(trace_path)` 鎴愬姛瀹屾垚涓€鏉″寘鍚涓姹傜殑浜嬩欢椹卞姩 trace
- **THEN** 绯荤粺 MUST 鍦?`report/` 鐩綍涓嬬敓鎴愪笌璇?trace 瀵瑰簲鐨?JSON 鏂囦欢鍜?CSV 鏂囦欢锛屼笖涓や唤鎶ュ憡鐨?request record 鏁伴噺 MUST 涓庤緭鍏?trace 璇锋眰鏁颁竴鑷?

## ADDED Requirements

### Requirement: CSV latency table flattens each request into a fixed timing row

CSV 鎶ュ憡 SHALL 浠ユ瘡涓?request 涓€琛岀殑鏂瑰紡杈撳嚭鍥哄畾鍒椼€傚垪鐨勯『搴?MUST 渚濇涓猴細`issue_time`銆乺eq_type`銆乧ompletion_time`銆乻q_wait_time`銆乸cie_request_send_time`銆乧ache_hit`銆乵apping_time`銆乼su_wait_time`銆乸hy_transfer_time`銆乸hy_array_time` 鍜?`pcie_return_and_status_time`銆傚浜?`SEARCH` 鍜?`COMPUTE`锛?`cache_hit` 鍒?MUST 涓?`/`銆傚浜庣紦瀛樺啓璇锋眰锛孋SV 鐨?completion_time` MUST 浣跨敤涓绘満鍙瀹屾垚鏃堕棿锛屼絾 `tsu_wait_time`銆乸hy_transfer_time` 鍜?`phy_array_time` MUST 浣跨敤鎸佷箙鍖栬矾寰勭殑鍚庣鏃堕棿銆?

#### Scenario: Read request row uses host-visible pipeline timings
- **WHEN** 涓€鏉?`READ` 璇锋眰琚啓鍏?CSV 鎶ュ憡
- **THEN** 璇ヨ姹傜殑 `sq_wait_time`銆乸cie_request_send_time`銆乵apping_time`銆乼su_wait_time`銆乸hy_transfer_time`銆乸hy_array_time` 鍜?`pcie_return_and_status_time` MUST 浠庝富鏈哄彲瑙佽矾寰勭殑璇锋眰 latency 鏁版嵁鎺ㄥ

#### Scenario: Buffered write row mixes host completion with persistence backend stages
- **WHEN** 涓€鏉?`WRITE` 璇锋眰鍏堝湪 cache 涓畬鎴愶紝鍚庣画鍐嶈 flush 鍒?flash
- **THEN** 璇ヨ姹傜殑 `completion_time` MUST 绛変簬涓绘満鍙瀹屾垚鏃堕棿锛屼絾 `tsu_wait_time`銆乸hy_transfer_time` 鍜?`phy_array_time` MUST 浣跨敤璇ヨ姹傜殑 persistence latency 鏁版嵁

### Requirement: CSV cache-hit and mapping columns distinguish CMT hits from other mapping paths

CSV 鎶ュ憡 MUST 鑳藉尯鍒?CMT cache hit銆丟MT/direct mapping resolution 鍜?mapping-read fallback銆傚浜庣湡姝?CMT hit 鐨勮姹傦紝`cache_hit` 鍒?MUST 鏄剧ず鍛戒腑鐘舵€侊紝涓?`mapping_time` MUST 涓?`0`銆傚浜庨渶瑕侀€氳繃 `MAPPING_READ` 绛夊緟鏄犲皠鐨勮姹傦紝`cache_hit` MUST 鏄剧ず鏈懡涓紝涓?`mapping_time` MUST 绛変簬璇锋眰鐨?AMU mapping wait 鏃堕暱銆?

#### Scenario: CMT hit reports zero mapping time
- **WHEN** 涓€鏉?`READ` 鎴?`WRITE` 璇锋眰鐨勫湴鍧€鏄犲皠鐩存帴鍛戒腑 `CMT`
- **THEN** CSV 琛屽繀椤绘妸 `cache_hit` 鍐欎负鍛戒腑鐘舵€侊紝骞跺皢 `mapping_time` 鍐欎负 `0`

#### Scenario: Mapping-read fallback reports miss and wait time
- **WHEN** 涓€鏉?`READ` 璇锋眰闇€瑕侀€氳繃 `MAPPING_READ` 鎵嶈兘鑾峰彇鐩爣 PPA
- **THEN** CSV 琛屽繀椤绘妸 `cache_hit` 鍐欎负鏈懡涓紝骞跺皢 `mapping_time` 鍐欎负闈為浂鐨?AMU mapping wait 鏃堕暱

### Requirement: CSV return/status latency includes response payload transfer for non-write requests

CSV 鎶ュ憡鐨?`pcie_return_and_status_time` 鍒?MUST 鍖呭惈璁锋眰瀹屾垚鐘舵€佽繑鍥炵殑 PCIe 鏃堕暱銆傚浜?`READ`銆乻EARCH` 鍜?`COMPUTE`锛屽畠杩樺繀椤昏鍏ュ搷搴旀暟鎹殑杩斿洖浼犺緭鏃堕暱锛屽嵆浣垮綋鍓嶆ā鎷熷櫒娌℃湁鏄惧紡鎶婅繖娈垫暟鎹繑鍥炲缓妯′负鍗曠嫭鐨?PCIe message銆傚浜?`WRITE` 鎴?`STATIC_WRITE`锛岃鍒?MUST 鍙寘鍚姸鎬佽繑鍥炶€屼笉鍖呭惈鏁版嵁杞緭鏃堕暱銆?

#### Scenario: Read row includes response data transfer
- **WHEN** 涓€鏉?`READ` 璇锋眰琚啓鍏?CSV 鎶ュ憡
- **THEN** 璇ヨ姹傜殑 `pcie_return_and_status_time` MUST 鍖呭惈璇昏繑鍥炴暟鎹殑浼犺緭鏃堕暱鍜屽畬鎴愮姸鎬佽繑鍥炴椂闂?

#### Scenario: Write row excludes response payload transfer
- **WHEN** 涓€鏉?`WRITE` 璇锋眰琚啓鍏?CSV 鎶ュ憡
- **THEN** 璇ヨ姹傜殑 `pcie_return_and_status_time` MUST 鍙寘鍚€?REQ_COMP` 绫荤姸鎬佽繑鍥炵殑 PCIe 鏃堕暱
