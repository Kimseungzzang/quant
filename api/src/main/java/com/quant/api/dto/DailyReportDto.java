package com.quant.api.dto;

import lombok.Builder;
import lombok.Getter;

import java.math.BigDecimal;
import java.time.LocalDate;

@Getter
@Builder
public class DailyReportDto {
    private LocalDate  date;
    private long       tradePairs;
    private long       wins;
    private long       losses;
    private double     winRate;
    private BigDecimal totalPnl;
    private BigDecimal maxPnl;
    private BigDecimal minPnl;
}
