package com.quant.api.dto;

import com.quant.api.domain.Trade;
import lombok.Builder;
import lombok.Getter;

import java.math.BigDecimal;
import java.time.OffsetDateTime;

@Getter
@Builder
public class TradeDto {

    private Long id;
    private OffsetDateTime tradedAt;
    private String stockCode;
    private String stockName;
    private String market;
    private String side;
    private Integer quantity;
    private BigDecimal price;
    private BigDecimal amount;
    private String mode;
    private String strategy;
    private BigDecimal realizedPnl;
    private BigDecimal pnlPct;

    public static TradeDto from(Trade t) {
        return TradeDto.builder()
            .id(t.getId())
            .tradedAt(t.getTradedAt())
            .stockCode(t.getStockCode())
            .stockName(t.getStockName())
            .market(t.getMarket())
            .side(t.getSide())
            .quantity(t.getQuantity())
            .price(t.getPrice())
            .amount(t.getAmount())
            .mode(t.getMode())
            .strategy(t.getStrategy())
            .realizedPnl(t.getRealizedPnl())
            .pnlPct(t.getPnlPct())
            .build();
    }
}
