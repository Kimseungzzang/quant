package com.quant.api.dto;

import com.quant.api.domain.AnalysisResult;
import lombok.Builder;
import lombok.Getter;

import java.math.BigDecimal;
import java.time.OffsetDateTime;

@Getter
@Builder
public class AnalysisResultDto {

    private Long id;
    private Long runId;
    private Integer rank;
    private String stockCode;
    private String stockName;
    private String market;
    private String horizon;
    private BigDecimal currentPrice;
    private BigDecimal changePct;
    private BigDecimal tradingValue;
    private BigDecimal finalScore;
    private BigDecimal winRatePct;
    private BigDecimal backtestReturn;
    private BigDecimal maxDrawdown;
    private Integer tradeCount;
    private OffsetDateTime createdAt;

    public static AnalysisResultDto from(AnalysisResult r) {
        return AnalysisResultDto.builder()
            .id(r.getId())
            .runId(r.getRun().getId())
            .rank(r.getRank())
            .stockCode(r.getStockCode())
            .stockName(r.getStockName())
            .market(r.getMarket())
            .horizon(r.getHorizon())
            .currentPrice(r.getCurrentPrice())
            .changePct(r.getChangePct())
            .tradingValue(r.getTradingValue())
            .finalScore(r.getFinalScore())
            .winRatePct(r.getWinRatePct())
            .backtestReturn(r.getBacktestReturn())
            .maxDrawdown(r.getMaxDrawdown())
            .tradeCount(r.getTradeCount())
            .createdAt(r.getCreatedAt())
            .build();
    }
}
