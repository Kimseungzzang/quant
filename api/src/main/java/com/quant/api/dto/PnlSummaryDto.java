package com.quant.api.dto;

import lombok.Builder;
import lombok.Getter;

import java.math.BigDecimal;

@Getter
@Builder
public class PnlSummaryDto {
    private BigDecimal totalRealizedPnl;
    private long totalTrades;
    private long winningTrades;
    private double winRate;            // 0~100
    private BigDecimal avgPnlPerTrade;
}
