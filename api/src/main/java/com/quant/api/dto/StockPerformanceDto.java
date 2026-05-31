package com.quant.api.dto;

import lombok.Builder;
import lombok.Getter;

import java.math.BigDecimal;

@Getter
@Builder
public class StockPerformanceDto {
    private String stockCode;
    private String stockName;
    private long   tradePairs;
    private long   wins;
    private double winRate;
    private BigDecimal totalPnl;
    private BigDecimal avgPnl;
    private BigDecimal maxPnl;
    private BigDecimal minPnl;
}
