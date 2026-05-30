package com.quant.api.dto;

import lombok.Getter;
import lombok.NoArgsConstructor;

@Getter
@NoArgsConstructor
public class CommandRequest {
    private String market = "domestic";
    private String horizon = "swing";
    private Integer topN = 10;
    private Integer lookbackDays;    // 분석 백테스트 기간 (null이면 서버 기본값)
    private String stockCode;
    private Integer periodDays = 60;
    private String startDate;   // YYYY-MM-DD (지정 시 periodDays 무시)
    private String endDate;     // YYYY-MM-DD (미지정 시 오늘)
}
